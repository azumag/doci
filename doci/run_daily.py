"""日次オーケストレータ: コーナー選択→台本(OpenCode Go)→音声(VOICEVOX)→映像(Minimax)→
合成(ffmpeg)→YouTubeアップロード(チャンネル別公開判定)→履歴記録。1回で1本生成。
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import warnings
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from . import (
    ai_text,
    assets,
    channel,
    compose,
    config,
    corners,
    history,
    imagegen,
    routing,
    topic_ledger,
    voicevox,
)
from .channel import ChannelSpec, CornerSpec


def _workdir_name(day: str, corner_key: str, hhmmss: str) -> str:
    """workdir名を組み立てる。`{day}_{corner_key}` プレフィックスは検索性のため維持しつつ、
    末尾に実行時刻を足して run ごとに一意にする（同日同コーナーの後続runによる上書き喪失を防ぐ）。"""
    return f"{day}_{corner_key}_{hhmmss}"


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def _real_publish_requested(do_upload: bool) -> bool:
    """投稿フラグと全体dry-runを合わせた、外部状態を確定する実行判定。"""
    return bool(do_upload and not config.PUBLISH_DRY_RUN)


def _publish_result_summary(results: list) -> list[dict[str, object]]:
    """外部投稿結果を、履歴・復旧用にboundedな値へ写す。"""
    return [
        {
            "platform": str(result.platform)[:40],
            "status": str(result.status)[:40],
            "id": str(result.id)[:200] if result.id else None,
            "detail": str(result.detail or "")[:240],
        }
        for result in results
    ][:12]


def _finalize_performance_application(
    spec: ChannelSpec,
    corner_key: str,
    decision_id: str,
    application_id: str | None,
    video_id: str | None,
    reservation_state: dict,
) -> str | None:
    if not application_id:
        return None
    if video_id:
        # 外部投稿済みの事実を、失敗し得る履歴書込みより先に立てる。
        # 書込み失敗時にapplicationをcancelして同じ仮説を再投稿しない。
        reservation_state["external_published"] = True
        history.apply_performance_decision(
            spec,
            corner_key,
            decision_id,
            application_id,
            video_id,
        )
        return application_id
    if reservation_state.get("external_unknown"):
        # 投稿結果不明（タイムアウト等）は実際には公開済みの可能性があるため、
        # topic_ledgerのpublishing状態と同様に取り消さず手動確認まで保留する。
        # 解消するまでcornerの次実験は適用されない
        # （performance_gated_publishのチャンネルは新規動画が公開されなくなる）。
        _log(
            "実績適用の結果が不明のため保留（要手動復旧）: "
            f"application_id={application_id} corner={corner_key} "
            "`python -m doci.run_daily --channel <id> "
            f"--recover-performance-application {application_id} "
            "--recovery-status <cancelled|published> [--recovery-video-id <id>]`"
        )
        return application_id
    history.cancel_performance_decision(
        spec,
        corner_key,
        decision_id,
        application_id,
        "YouTube投稿が成功しなかったため仮説を未消費に戻す",
    )
    reservation_state.pop("performance_application_id", None)
    return None


def _credits(spec: ChannelSpec, corner) -> str:
    """概要欄に付ける素材クレジット。VOICEVOX はキャラ名込みで表記必須（利用規約）。
    Pexels は必須ではないが明記する。"""
    import re as _re

    label = spec.voice_for(corner).label
    m = _re.search(r"[（(]\s*([^/／）)]+)", label)  # 「メリケンAI (冥鳴ひまり/ノーマル)」→ 冥鳴ひまり
    char = m.group(1).strip() if m else ""
    vv = f"VOICEVOX:{char}" if char else "VOICEVOX"
    voicevox_credit = f"音声合成: {vv}（https://voicevox.hiroshiba.jp/）"
    asset_credit = "背景・映像素材: Pexels（https://www.pexels.com/）"
    template = spec.style.credits.template
    if not template:
        return (
            "\n\n──────────\n"
            "■ クレジット / Credits\n"
            f"{voicevox_credit}\n"
            f"{asset_credit}"
        )
    try:
        rendered = template.format(
            voicevox_credit=voicevox_credit,
            asset_credit=asset_credit,
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid style.credits.template: {exc}") from exc
    if voicevox_credit not in rendered:
        warnings.warn(
            "style.credits.template omitted {voicevox_credit}; required credit appended",
            UserWarning,
            stacklevel=2,
        )
        rendered = rendered.rstrip() + "\n" + voicevox_credit
    return "\n\n" + rendered.lstrip()


def _apply_title_pattern_check(
    spec: ChannelSpec,
    script: dict,
    recent_titles_for_prompt: list[str],
    cooldown_days: int,
) -> None:
    """生成済みタイトルの修辞パターン重複を検出し、scriptへ記録する(issue #37)。

    題材レベルのcooldownだけでは、題材(具体的な事実・数値)が違っても
    タイトルの型（固有名詞+問題語+疑問形/煽り構文）が繰り返される使い回し
    を検出できない。まずは検出・記録に留め、公開判断は変えない
    （数値効果を推測せず、後日のCTR比較へつなげる）。
    """
    if not (spec.pipeline_get("title_pattern_check", False) and cooldown_days > 0):
        return
    try:
        title_pattern_match = ai_text.check_title_pattern_duplicate(
            str(script.get("title", "")), recent_titles_for_prompt
        )
    except Exception as exc:  # noqa: BLE001 判定不調で生成全体を止めない
        _log(f"タイトル修辞パターン判定に失敗→記録なしで継続: {str(exc)[:160]}")
        title_pattern_match = None
    script["_title_pattern_check"] = {
        "checked": True,
        "match": title_pattern_match,
    }
    if title_pattern_match is not None:
        axes = "/".join(title_pattern_match["overlapping_axes"]) or "型"
        _log(
            "タイトル修辞パターン重複の疑い: "
            f"「{script.get('title', '')}」≈「{title_pattern_match['matched_title']}」"
            f"（{axes}／確信度{title_pattern_match['confidence']:.2f}）: "
            f"{title_pattern_match['reason']}"
        )


def _apply_narration_pattern_check(
    spec: ChannelSpec,
    script: dict,
    recent_openings: list[str],
) -> None:
    """生成済みnarrationの書き出し修辞パターン重複を検出し、scriptへ記録する(issue #70)。

    ai_text.generate()内のLayer2(正規表現ファミリー)が拾えない未知の重複パターンを
    検出・記録するためだけのもので、公開判断は変えない
    （_apply_title_pattern_checkと同じ検出・記録のみの運用）。
    """
    if not (spec.pipeline_get("narration_pattern_check", False) and recent_openings):
        return
    opening = ai_text._opening_sentence(str(script.get("narration", "")))
    try:
        opening_pattern_match = ai_text.check_narration_opening_pattern_duplicate(
            opening, recent_openings
        )
    except Exception as exc:  # noqa: BLE001 判定不調で生成全体を止めない
        _log(f"書き出しパターン判定に失敗→記録なしで継続: {str(exc)[:160]}")
        opening_pattern_match = None
    script["_narration_opening_check"] = {
        "checked": True,
        "match": opening_pattern_match,
    }
    if opening_pattern_match is not None:
        axes = "/".join(opening_pattern_match["overlapping_axes"]) or "型"
        _log(
            "書き出し修辞パターン重複の疑い: "
            f"「{opening}」≈「{opening_pattern_match['matched_opening']}」"
            f"（{axes}／確信度{opening_pattern_match['confidence']:.2f}）: "
            f"{opening_pattern_match['reason']}"
        )


def _apply_ambiguous_date_title_check(spec: ChannelSpec, script: dict) -> None:
    """タイトルの過去年月・「改訂」「最新」表現と日付根拠の不整合を検出し、scriptへ記録する(issue #57)。

    正規表現のみで通信・LLMを伴わないため誤動作しにくく、既存2関数のような
    try/exceptガードは不要。検出・記録のみで公開判断は変えない
    （_apply_title_pattern_checkと同じ運用）。
    """
    if not spec.pipeline_get("ambiguous_date_title_check", False):
        return
    research = script.get("_research")
    facts = research.get("facts") if isinstance(research, dict) else None
    match = ai_text.check_ambiguous_date_title(str(script.get("title", "")), facts)
    script["_ambiguous_date_title_check"] = {"checked": True, "match": match}
    if match is not None and not match["supported"]:
        _log(
            "曖昧日付タイトルの疑い: "
            f"「{script.get('title', '')}」({'/'.join(match['matched_texts'])}) "
            f"不足根拠: {', '.join(match['missing']) or match['reason']}"
        )


def _apply_youtube_engagement_actions(
    spec: ChannelSpec, corner: CornerSpec, script: dict, video_id: str
) -> None:
    """公開直後のYouTube動画に再生リスト追加・討論誘発コメント投稿を行う(issue #86)。

    いずれもpipeline設定で個別に無効化でき、失敗しても動画生成・投稿本体は
    止めない(ソフトフェイル)。コメントの「固定」自体はYouTube Data APIに
    無いため、投稿はできても固定は手動操作が必要(youtube.post_commentのdocstring参照)。
    """
    from . import youtube

    if spec.pipeline_get("youtube_auto_playlist", False):
        try:
            playlist_id = youtube.ensure_playlist(
                corner.label,
                token_file=spec.publish.youtube.token,
                client_secret_file=spec.publish.youtube.client_secret,
            )
            result = youtube.add_video_to_playlist(
                playlist_id,
                video_id,
                token_file=spec.publish.youtube.token,
                client_secret_file=spec.publish.youtube.client_secret,
            )
            _log(f"再生リスト追加 ({corner.label}): {result}")
        except Exception as e:  # noqa: BLE001 - 非致命的な後処理
            _log(f"再生リスト追加失敗（投稿は継続）: {e}")

    if spec.pipeline_get("youtube_auto_engagement_comment", False):
        try:
            comment_text = ai_text.generate_engagement_comment(corner, script)
            if not comment_text:
                _log("討論誘発コメント生成に失敗→投稿スキップ")
            else:
                youtube.post_comment(
                    video_id,
                    comment_text,
                    token_file=spec.publish.youtube.token,
                    client_secret_file=spec.publish.youtube.client_secret,
                )
                _log(
                    f"討論誘発コメント投稿: {comment_text}"
                    "（固定はYouTube Studioで手動操作してください）"
                )
        except Exception as e:  # noqa: BLE001 - 非致命的な後処理
            _log(f"コメント投稿失敗（投稿は継続）: {e}")


def _run_once(
    spec: ChannelSpec,
    day: str,
    corner_key: str | None,
    do_upload: bool,
    video_scenes: int,
    reservation_state: dict,
) -> dict:
    real_publish = _real_publish_requested(do_upload)
    if corner_key and corner_key not in spec.corners:
        raise ValueError(f"unknown corner for channel {spec.id}: {corner_key}")
    if real_publish:
        topic_ledger.ensure_daily_capacity(spec)
    corner_candidates = (
        [spec.corners[corner_key]]
        if corner_key
        else corners.rotation_order(spec, history.last_corner(spec))
    )
    eval_window_hours = 0
    if real_publish and spec.pipeline_get("performance_feedback", False):
        eval_window_hours = int(
            spec.pipeline_get("performance_eval_window_hours", 0) or 0
        )
    corner = None
    eval_window_check = None
    last_skip_exc: history.PerformanceEvalWindowSkip | None = None
    for candidate in corner_candidates:
        if eval_window_hours <= 0:
            corner = candidate
            break
        try:
            eval_window_check = history.ensure_corner_eval_capacity(
                spec, candidate.key, eval_window_hours
            )
        except history.PerformanceEvalWindowSkip as exc:
            last_skip_exc = exc
            continue
        corner = candidate
        break
    if corner is None:
        # corner_keyを明示した場合はcandidatesが1件のため単純に再送出。
        # 自動選択の場合はrotation全corner分（各cornerは独立した実験を
        # 持つ）を試したうえで、それでも空きが無いときだけスキップする
        # （評価待ちの1cornerだけで他cornerの投稿枠まで奪わないため）。
        _log(
            f"実験評価期間スキップ: rotation全{len(corner_candidates)}corner中"
            f"評価期間内でないcornerが無い（最後に確認: {last_skip_exc.reason}）"
        )
        raise last_skip_exc
    if (
        eval_window_check is not None
        and eval_window_check["active"]
        and eval_window_check["elapsed_hours"] is None
    ):
        _log(
            f"実験評価期間チェック: corner={corner.key} "
            f"ts不明のため経過時間を判定できず生成を継続"
        )
    voice = spec.voice_for(corner)
    workdir = spec.output_dir / _workdir_name(
        day, corner.key, datetime.now().strftime("%H%M%S")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    max_uploads_per_day = spec.pipeline_get("max_uploads_per_day")
    _log(
        f"channel={spec.id} corner={corner.key} "
        f"voice={corner.voice_key}(spk{voice.speaker}) workdir={workdir}"
    )
    _log(
        "投稿頻度policy: "
        f"max_uploads_per_day={max_uploads_per_day if max_uploads_per_day is not None else '無制限'} "
        f"performance_eval_window_hours={spec.pipeline_get('performance_eval_window_hours', 0)}"
    )

    # 1) 台本
    performance_decision = None
    performance_application_id: str | None = None
    if spec.pipeline_get("performance_feedback", False):
        try:
            from . import performance

            performance_decision = performance.refresh(spec, corner_key=corner.key)
            if performance_decision["status"] == "active" and real_publish:
                performance_application_id = history.reserve_performance_decision(
                    spec,
                    corner.key,
                    performance_decision["decision_id"],
                    hypothesis=performance.decision_hypothesis(performance_decision),
                )
                if performance_application_id:
                    reservation_state.update(
                        {
                            "performance_spec": spec,
                            "performance_corner": corner.key,
                            "performance_decision_id": performance_decision[
                                "decision_id"
                            ],
                            "performance_application_id": performance_application_id,
                        }
                    )
                else:
                    performance_decision = {
                        **performance_decision,
                        "status": "waiting",
                        "reason": (
                            "同じdecisionは別runが適用予約済み。新しい指標snapshotを待つ"
                        ),
                        "guidance": "",
                    }
            _log(
                "実績フィードバック: "
                f"{performance_decision['status']} "
                f"(decision={performance_decision['decision_id']}; "
                f"{performance_decision['reason']})"
            )
        except Exception as exc:  # readback不調でも通常生成は継続
            _log(f"実績フィードバック取得失敗→なしで継続: {str(exc)[:240]}")
    _log("台本生成 (OpenCode Go / qwen3.7-plus)…")
    cooldown_days = int(
        spec.pipeline_get("topic_cooldown_days", config.TOPIC_COOLDOWN_DAYS)
    )
    reservation_id: str | None = None
    topic_ledger_reservation_id: str | None = None
    selected_topic = ""
    selected_topic_metadata: dict[str, object] = {}

    def capture_topic_metadata(research: dict) -> None:
        nonlocal selected_topic_metadata
        if isinstance(research, dict):
            selected_topic_metadata = dict(research)

    def semantic_duplicate_check(
        candidate_topic: str, recent_topics: list[str]
    ) -> history.TopicMatch | None:
        # 文字列照合が0件のときだけ呼ばれる。語彙が一致しない比喩の言い換え重複を
        # LLMで補助判定する（通信・応答不良時はNoneを返し見逃し側に倒す）。
        try:
            matched = ai_text.check_semantic_duplicate(candidate_topic, recent_topics)
        except Exception as exc:  # noqa: BLE001 判定不調で生成全体を止めない
            _log(f"意味的重複判定に失敗→スキップせず継続: {str(exc)[:160]}")
            return None
        if matched is None:
            return None
        matched_topic, confidence = matched
        return history.TopicMatch(
            topic=matched_topic, ts="", similarity=confidence, source="LLM判定"
        )

    def reserve_selected_topic(topic: str) -> None:
        nonlocal reservation_id, selected_topic, topic_ledger_reservation_id
        selected_topic = topic.strip()
        try:
            # topic_ledgerはpipeline.max_uploads_per_dayのJST日次実投稿枠だけを見る
            # (チャンネル間でテーマは十分に異なるため、題材の跨ぎ照合は行わない設計)。
            # 題材内容の重複判定は、この後のhistory.reserve_topic()がチャンネル別に行う。
            topic_ledger_reservation_id = topic_ledger.reserve(
                spec,
                corner.key,
                selected_topic,
                metadata=selected_topic_metadata,
                reserve=real_publish,
            )
            if topic_ledger_reservation_id:
                reservation_state.update(
                    {
                        "topic_ledger_spec": spec,
                        "topic_ledger_corner": corner.key,
                        "topic_ledger_topic": selected_topic,
                        "topic_ledger_metadata": selected_topic_metadata,
                        "topic_ledger_reservation_id": topic_ledger_reservation_id,
                    }
                )
            reservation_id = history.reserve_topic(
                spec,
                corner.key,
                selected_topic,
                cooldown_days=cooldown_days,
                reserve=real_publish,
                metadata=selected_topic_metadata,
                topic_ledger_reservation_id=topic_ledger_reservation_id,
                semantic_check=(
                    semantic_duplicate_check if cooldown_days > 0 else None
                ),
            )
            if reservation_id:
                reservation_state.update(
                    {
                        "spec": spec,
                        "corner": corner.key,
                        "topic": selected_topic,
                        "reservation_id": reservation_id,
                        "topic_metadata": selected_topic_metadata,
                    }
                )
        except history.TopicCooldownSkip as exc:
            _log(f"題材スキップ: {exc.reason}")
            if topic_ledger_reservation_id:
                # topic_ledger.reserve()は照合を行わずqueued行を無条件に書くため、
                # 直後のhistory.reserve_topic()がチャンネル内重複でスキップした場合、
                # ここで取り消さないと日次投稿枠を消費したまま残る。呼び出し元
                # (ai_text.pyの構成プラン再設計ループ)はこの例外を最終試行以外
                # 再送出しないため、外側run()の例外時クリーンアップに頼れない。
                try:
                    topic_ledger.cancel(
                        spec,
                        corner.key,
                        selected_topic,
                        topic_ledger_reservation_id,
                        f"チャネル内題材重複によりスキップ: {exc.reason}",
                        metadata=selected_topic_metadata,
                    )
                except Exception as cleanup_exc:  # noqa: BLE001 元のスキップ判定を隠さない
                    _log(f"共通題材台帳の取消失敗: {cleanup_exc}")
                reservation_state.pop("topic_ledger_spec", None)
                reservation_state.pop("topic_ledger_corner", None)
                reservation_state.pop("topic_ledger_topic", None)
                reservation_state.pop("topic_ledger_metadata", None)
                reservation_state.pop("topic_ledger_reservation_id", None)
                topic_ledger_reservation_id = None
            raise
        if cooldown_days > 0:
            mode = "キュー予約" if real_publish else "dry-run照合"
            _log(f"題材cooldown: {cooldown_days}日 / {mode}「{selected_topic}」")

    recent_titles_for_prompt = history.recent_titles(spec, cooldown_days=cooldown_days)
    # issue #70レビュー指摘: 両フラグ無効のチャンネルでも毎回history全完了行の
    # script.jsonを読み込むのは無駄なI/Oのため、どちらか有効な場合だけ取得する。
    narration_opening_features_enabled = spec.pipeline_get(
        "narration_opening_guard", False
    ) or spec.pipeline_get("narration_pattern_check", False)
    recent_openings_for_prompt = (
        history.recent_narration_openings(spec, corner.key)
        if narration_opening_features_enabled
        else []
    )
    script = ai_text.generate(
        spec,
        corner,
        day,
        recent_titles_for_prompt,
        topic_guard=reserve_selected_topic,
        topic_metadata_guard=capture_topic_metadata,
        performance_decision=performance_decision,
        recent_openings=recent_openings_for_prompt,
    )
    _apply_title_pattern_check(
        spec, script, recent_titles_for_prompt, cooldown_days
    )
    _apply_narration_pattern_check(spec, script, recent_openings_for_prompt)
    _apply_ambiguous_date_title_check(spec, script)
    if eval_window_check is not None:
        script["_performance_eval_window"] = eval_window_check
    if spec.pipeline_get("performance_gated_publish", False):
        theme_assessment = None
        youtube_privacy = "public" if performance_application_id else "unlisted"
        script["_performance_gated_publish"] = {
            "applied": bool(performance_application_id),
            "privacy": youtube_privacy,
            "decision_id": (
                performance_decision.get("decision_id")
                if performance_decision
                else None
            ),
        }
        if performance_application_id:
            _log(
                "実績フィードバック公開ゲート: 施策適用runのためpublic "
                f"(decision={script['_performance_gated_publish']['decision_id']})"
            )
        else:
            status = performance_decision.get("status") if performance_decision else None
            reason = performance_decision.get("reason") if performance_decision else None
            _log(
                "実績フィードバック公開ゲート: 施策未適用のためunlisted "
                f"(status={status}; {reason})"
            )
    else:
        from . import youtube_review

        youtube_privacy, theme_assessment = youtube_review.choose_privacy(spec, script)
        if theme_assessment is not None:
            script["_youtube_theme_review"] = theme_assessment.to_dict()
            if theme_assessment.eligible_for_public:
                _log("YouTube主題ガード: 3項目と主題適合が明確→public")
            else:
                _log(
                    "YouTube主題ガード: "
                    + " / ".join(theme_assessment.reasons)
                    + "→unlisted"
                )
    (workdir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"title: {script['title']}  (narration {len(script['narration'])}字 / scenes {len(script['scenes'])})")

    # 2) 音声（voices.json の話者＋速度/ピッチ/抑揚/音量を適用: issue #1）
    _log("音声合成 (VOICEVOX)…")
    v = voice
    tts = voicevox.synthesize(
        script["narration"], v.speaker, workdir / "narration.wav",
        speed=v.speed, pitch=v.pitch, intonation=v.intonation,
        intonation_vary=v.intonation_vary, volume=v.volume,
    )
    _log(f"narration {tts.duration:.1f}s (spk{v.speaker} speed{v.speed} into{v.intonation})")

    # 2.5) 尺が決まったので向き・サイズを決める。longform(>180s=YouTube通常動画)は横16:9、
    #      ショートは縦9:16。以降の素材取得・合成・AI生成へ同じ寸法/向きを流す。
    route = routing.classify(tts.duration)
    out_w, out_h, orientation = routing.output_spec(route, config.VIDEO_WIDTH, config.VIDEO_HEIGHT)
    aspect = "16:9" if route.landscape else "9:16"
    _log(f"出力: {orientation} {out_w}x{out_h} (tier={route.tier})")

    # 3) 映像（尺連動で画像枚数を増やす: issue #4）
    #    短尺は台本のシーン数のまま。長尺はシーンのプロンプトを順序保存で使い回し、
    #    1枚あたり約 SECONDS_PER_IMAGE 秒になるよう枚数を増やして間延びを防ぐ。
    scenes_meta = script["scenes"]
    n_scenes = len(scenes_meta)
    seconds_per_image = float(
        spec.pipeline_get("seconds_per_image", config.SECONDS_PER_IMAGE)
    )
    max_images = int(spec.pipeline_get("max_images", config.MAX_IMAGES))
    asset_media = str(spec.pipeline_get("asset_media", config.ASSET_MEDIA))
    target = math.ceil(tts.duration / seconds_per_image) if tts.duration > 0 else n_scenes
    n_images = max(n_scenes, min(target, max_images))
    if n_images > n_scenes:
        _log(f"映像スケール: {n_scenes}シーン→{n_images}枚 (約{tts.duration / n_images:.0f}s/枚)")
    use_video = config.VIDEO_BACKEND == "minimax" and video_scenes > 0
    scene_objs: list[compose.Scene] = []
    occ: dict[int, int] = {}
    chart_cache: dict[int, bool] = {}  # 図表は si ごとに1シーンに統合（重複スロットをスキップ）
    # サムネイル背景選定用: シーンごとの「主画」(k==0で取得した実素材)のパス/動画フラグを記録。
    primary_assets: dict[int, tuple] = {}
    # 画像スロットの配分をビート重要度(act: 起承転結)で重み付け（issue: 単純比例だと山場も
    # 前振りも同じ枚数になり間延びする）。act未指定(空文字)は重み1.0＝現行の均等配分と完全一致。
    # 図表シーンは1スロットしか実描画されない(chart_cacheで統合)ため重み付けしても無駄なので1.0固定。
    _ACT_WEIGHT = {"起": 0.85, "承": 1.0, "転": 1.3, "結": 0.95}
    weights = [
        1.0 if sm.get("chart") else _ACT_WEIGHT.get(sm.get("act", ""), 1.0)
        for sm in scenes_meta
    ]
    cum_w = [0.0]
    for w in weights:
        cum_w.append(cum_w[-1] + w)
    total_w = cum_w[-1] or float(n_scenes)
    for j in range(n_images):
        # 左端サンプリング（旧 `j * n_scenes // n_images` と同じ基準点）。中心点(+0.5)サンプリングは
        # 均等重み時でも旧式と異なる si を選ぶことがある（実測で確認済み）ため使わない。
        pos = j * total_w / n_images
        si = n_scenes - 1
        for i in range(n_scenes):
            if cum_w[i] <= pos < cum_w[i + 1]:
                si = i
                break
        k = occ.get(si, 0)
        occ[si] = k + 1
        sm = scenes_meta[si]
        # 図表シーン（issue #2）: Pexsels/AIを使わず HTML→画像で描画し、静止表示する。
        if sm.get("chart"):
            # 図表アニメはシーン尺に合わせて compose 側で描画（spec を渡すだけ）。
            # 背景は「テーマ＋内容」から都度選定・取得（キャッシュあり）。
            # 同一図表が複数スロットに割り当たっても1シーンに統合し、再アニメを防ぐ。
            if si in chart_cache:
                continue
            chart_cache[si] = True
            from . import chart_bg
            theme = f"{script.get('title', '')} / {script.get('description', '')}"[:180]
            try:
                chart_spec = chart_bg.ensure(sm["chart"], theme, workdir, si)
            except Exception as e:
                _log(f"図表背景の選定/取得に失敗（背景なしで継続）: {e}")
                chart_spec = sm["chart"]
            scene_objs.append(compose.Scene(path=workdir, is_video=False,
                                            caption=sm.get("caption", ""), chart_spec=chart_spec))
            _log(f"図表シーン (scene{si}, {sm['chart'].get('type')}) ← 背景付き・尺合わせ描画")
            continue
        img = workdir / f"scene_{si:02d}_{k}.png"
        base_prompt = sm.get("visual_prompt") or sm.get("caption") or "abstract background"
        # 1) まず実フリー素材を当てる（issue #9）。variant=k で同一シーンは別候補を選ぶ。
        #    ASSET_MEDIA=mix はシーン主画(k=0)を動画、使い回し(k>0)を写真に。video は全て動画優先。
        #    動画→写真→AI生成 の順に、各段が独立に劣化フォールバックする。
        got_path, is_video = None, False
        if config.ASSET_BACKEND not in ("", "none"):
            want_video = asset_media == "video" or (asset_media == "mix" and k == 0)
            if want_video:
                try:
                    vid = workdir / f"scene_{si:02d}_{k}.mp4"
                    got = assets.fetch_video(
                        base_prompt, vid, width=out_w, height=out_h, orientation=orientation, variant=k
                    )
                    if got is not None:
                        got_path, is_video = got, True
                        _log(f"素材取得(動画) {j + 1}/{n_images} (scene{si} var{k}, pexels)")
                except Exception as e:  # 動画失敗は写真へ
                    _log(f"動画取得失敗: {e} → 写真へ")
            if got_path is None:  # 写真モード or 動画が無かった/失敗
                try:
                    got = assets.fetch_image(
                        base_prompt, img, width=out_w, height=out_h, orientation=orientation, variant=k
                    )
                    if got is not None:
                        got_path = got
                        _log(f"素材取得(写真) {j + 1}/{n_images} (scene{si} var{k}, pexels)")
                except Exception as e:  # 写真失敗はAI生成へ
                    _log(f"写真取得失敗: {e} → AI生成へ")
        # 2) 素材が無ければAI生成（構図変化語を足して使い回しの単調を避ける）。
        if got_path is None:
            vprompt = base_prompt
            if k > 0:
                vprompt = f"{base_prompt}, alternate camera angle and composition, variation {k + 1}"
            _log(f"画像生成 {j + 1}/{n_images} (scene{si} var{k}, {config.IMAGE_BACKEND})…")
            try:
                imagegen.generate_image(vprompt, img, aspect_ratio=aspect)
                got_path = img
            except Exception as e:  # AI生成も不可(例: Gemini課金停止)→直前の素材を流用して継続
                if scene_objs:
                    _log(f"AI生成失敗: {e} → 直前の素材を流用")
                    prev = scene_objs[-1]
                    got_path, is_video = prev.path, prev.is_video
                else:
                    raise
        path = got_path
        # Minimax動画化は、既にPexsels動画でない静止画(is_video=False)に対してのみ。
        if use_video and not is_video and k == 0 and si < video_scenes:
            try:
                from . import minimax
                _log(f"動画生成 scene{si} (Minimax Hailuo)… 数分かかります")
                mp4 = workdir / f"scene_{si:02d}.mp4"
                vprompt2 = (sm["visual_prompt"] + " " + sm.get("motion", "")).strip()
                minimax.generate_video(vprompt2, mp4, first_frame_image=img)
                path, is_video = mp4, True
            except Exception as e:  # 動画失敗時は静止画にフォールバック
                _log(f"動画生成失敗→静止画にフォールバック: {e}")
        scene_objs.append(compose.Scene(path=path, is_video=is_video, caption=sm.get("caption", ""), motion=sm.get("motion", "")))
        if k == 0:
            primary_assets[si] = (path, is_video)

    # 4) 合成（2.5で決めた向き・サイズで）
    _log("合成 (ffmpeg)…")
    out_mp4 = workdir / "video.mp4"
    bgm_path = channel.bgm_path(spec, corner, day)
    compose.compose(
        scene_objs, tts.wav_path, tts.duration, out_mp4,
        bgm=bgm_path, segments=tts.segments,
        width=out_w, height=out_h,
        style=spec.style,
    )
    _log(f"動画完成: {out_mp4} ({out_mp4.stat().st_size} bytes)")

    # 4.5) 配信ルーティング（route は 2.5 で算出済み: issue #3）
    _log(
        f"ルート: {route.tier} ({tts.duration:.0f}s) {orientation} "
        f"youtube_short={route.is_youtube_short} 推奨={'/'.join(route.platforms)}"
    )

    # 4.6) サムネイル生成（縦タイトルカードを作り、API送信直前だけ16:9ピラーボックス化）。
    #      失敗しても動画生成・投稿自体は止めない。
    thumbnail_path = None
    try:
        from . import thumbnail
        # act重みが最大の非チャートシーンの実素材を背景に選ぶ
        non_chart_candidates = [
            si for si in primary_assets
            if not scenes_meta[si].get("chart")
        ]
        if non_chart_candidates:
            best_si = max(non_chart_candidates, key=lambda si: weights[si])
            bg_path, bg_is_video = primary_assets[best_si]
            bg_image = bg_path
            if bg_is_video:
                frame_path = workdir / "thumb_bg_frame.jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(bg_path), "-ss", "0.5", "-frames:v", "1", "-q:v", "2", str(frame_path)],
                    capture_output=True, timeout=30,
                )
                bg_image = frame_path if frame_path.exists() else None
        else:
            bg_image = None
        thumb_vertical = workdir / "thumbnail_vertical.png"
        thumbnail.render(
            script["title"],
            thumb_vertical,
            bg_image=bg_image,
            width=out_w,
            height=out_h,
            style=spec.style.thumbnail,
        )
        thumb_final = workdir / "thumbnail.png"
        thumbnail.to_16x9(thumb_vertical, thumb_final)
        thumbnail_path = thumb_final
        _log(f"サムネイル生成: {thumb_final}")
    except Exception as e:
        _log(f"サムネイル生成失敗（動画は続行）: {e}")

    # 5) アップロード（route.platforms と各 PUBLISH_* で出し分け: issue #3）
    video_id = None
    pub_results: list = []
    if do_upload:
        if real_publish:
            if topic_ledger_reservation_id:
                topic_ledger.mark_publishing(
                    spec,
                    corner.key,
                    selected_topic,
                    topic_ledger_reservation_id,
                    metadata=selected_topic_metadata,
                )
            if reservation_id:
                history.mark_topic_publishing(
                    spec,
                    corner.key,
                    selected_topic,
                    reservation_id,
                    metadata=selected_topic_metadata,
                    topic_ledger_reservation_id=topic_ledger_reservation_id,
                    workdir=workdir,
                )
            reservation_state["topic_stage"] = "publishing"
        from . import publish
        _log(f"投稿 (route={route.tier} → {'/'.join(route.platforms)})…")
        pub_results = publish.publish(
            out_mp4,
            title=script["title"],
            description=script["description"] + _credits(spec, corner),
            tags=script.get("tags", []),
            route=route,
            spec=spec,
            thumbnail=thumbnail_path,
            youtube_privacy=youtube_privacy,
        )
        publish_summary = _publish_result_summary(pub_results)
        if topic_ledger_reservation_id:
            # どのプラットフォームまで外部へ到達したかを、結果不明の間も
            # 共通台帳へ残す。外部再確認時の重複投稿防止に使う。
            topic_ledger.mark_publishing(
                spec,
                corner.key,
                selected_topic,
                topic_ledger_reservation_id,
                metadata=selected_topic_metadata,
                publish_results=publish_summary,
            )
        for r in pub_results:
            _log(f"  {r.platform}: {r.status}{(' ' + (r.url or r.detail)) if (r.url or r.detail) else ''}")
            if r.platform == "youtube" and r.status == "ok":
                video_id = r.id
        if any(result.status == "unknown" for result in pub_results):
            # APIが受理した直後のタイムアウトも含め、成功IDが無い限り
            # publishingを解除しない。重複投稿より手動確認を優先する。
            reservation_state["external_unknown"] = True
        if performance_application_id and performance_decision:
            performance_application_id = _finalize_performance_application(
                spec,
                corner.key,
                performance_decision["decision_id"],
                performance_application_id,
                video_id,
                reservation_state,
            )
        # 外部投稿が1件でも成功した後は、後続の履歴詳細保存が失敗しても
        # queued予約をcancelしない。公開済み題材の再投稿防止を優先する。
        if any(result.status == "ok" for result in pub_results):
            reservation_state["external_published"] = True
        if video_id:
            _apply_youtube_engagement_actions(spec, corner, script, video_id)
    else:
        _log("アップロードはスキップ (--no-upload)")

    # 6) 履歴
    has_published = any(result.status == "ok" for result in pub_results)
    has_unknown = any(result.status == "unknown" for result in pub_results)
    final_status = (
        "publishing"
        if has_unknown
        else "published"
        if has_published
        else "generated"
    )
    publish_summary = _publish_result_summary(pub_results)
    if topic_ledger_reservation_id and final_status != "publishing":
        # 外部投稿成功後の履歴詳細保存より先に確定する。後段で落ちても
        # 共通台帳は公開済み題材を再利用不可として保持する。
        topic_ledger.complete(
            spec,
            corner.key,
            selected_topic,
            topic_ledger_reservation_id,
            status=final_status,
            metadata=selected_topic_metadata,
            video_id=video_id,
            publish_results=publish_summary,
        )
        reservation_state["topic_stage"] = final_status
    history.record(
        spec,
        corner.key,
        script["title"],
        video_id,
        extra={
            "status": final_status,
            "topic": selected_topic,
            "topic_concepts": history.topic_concepts(selected_topic),
            "topic_metadata": history.topic_metadata(
                selected_topic,
                selected_topic_metadata,
            ),
            "reservation_id": reservation_id,
            "topic_ledger_reservation_id": topic_ledger_reservation_id,
            "performance_decision_id": (
                performance_decision["decision_id"]
                if performance_application_id and performance_decision
                else None
            ),
            "performance_application_id": performance_application_id,
            "workdir": str(workdir),
            "description": script.get("description", ""),
            "duration_sec": round(tts.duration, 1),
            "tier": route.tier,
            "platforms": route.platforms,
            "publish": publish_summary,
            "youtube_privacy": youtube_privacy if video_id else None,
            "youtube_theme_review": (
                theme_assessment.to_dict() if theme_assessment is not None else None
            ),
        },
    )
    media_cleanup: dict[str, object]
    from . import output_cleanup

    if output_cleanup.publish_results_complete(pub_results):
        recovery = {
            "script": "script.json",
            "channel": spec.id,
            "corner": corner.key,
            "date": day,
            "title": script["title"],
            "video_id": video_id,
            "voice": {
                "key": corner.voice_key,
                "speaker": v.speaker,
                "speed": v.speed,
                "pitch": v.pitch,
                "intonation": v.intonation,
                "intonation_vary": v.intonation_vary,
                "volume": v.volume,
                "label": v.label,
            },
            "render": {
                "duration_sec": round(tts.duration, 1),
                "tier": route.tier,
                "platforms": route.platforms,
                "orientation": orientation,
                "width": out_w,
                "height": out_h,
                "video_scenes": video_scenes,
                "seconds_per_image": seconds_per_image,
                "max_images": max_images,
                "asset_media": asset_media,
                "image_backend": config.IMAGE_BACKEND,
                "video_backend": config.VIDEO_BACKEND,
                "bgm": str(bgm_path) if bgm_path else None,
            },
            "publish": publish_summary,
        }
        try:
            cleanup_result = output_cleanup.cleanup_workdir(
                spec.output_dir,
                workdir,
                apply=True,
                recovery=recovery,
            )
        except Exception as exc:  # 投稿成功をcleanup失敗で失敗扱いにしない
            media_cleanup = {
                "status": "error",
                "files": 0,
                "bytes": 0,
                "error": str(exc)[:240],
            }
            _log(
                "アップロード後の媒体整理を完了確認できません"
                f"（workdirを要確認）: {str(exc)[:240]}"
            )
        else:
            media_cleanup = cleanup_result.to_dict()
            _log(
                "アップロード後の媒体削除: "
                f"{cleanup_result.files} files / {cleanup_result.bytes} bytes"
            )
    else:
        statuses = [str(result.status) for result in pub_results]
        media_cleanup = {
            "status": "retained",
            "files": 0,
            "bytes": 0,
            "reason": (
                "upload results require retry or confirmation"
                if statuses
                else "no completed upload"
            ),
            "publish_statuses": statuses,
        }
    reservation_state["finalized"] = True
    return {
        "channel": spec.id,
        "corner": corner.key,
        "title": script["title"],
        "video": str(out_mp4),
        "video_id": video_id,
        "duration_sec": round(tts.duration, 1),
        "tier": route.tier,
        "platforms": route.platforms,
        "publish": [{"platform": r.platform, "status": r.status} for r in pub_results],
        "youtube_privacy": youtube_privacy if video_id else None,
        "workdir": str(workdir),
        "video_retained": out_mp4.exists(),
        "media_cleanup": media_cleanup,
    }


def run(
    spec: ChannelSpec,
    day: str,
    corner_key: str | None,
    do_upload: bool,
    video_scenes: int,
) -> dict:
    reservation_state: dict = {}
    try:
        return _run_once(
            spec,
            day,
            corner_key,
            do_upload,
            video_scenes,
            reservation_state,
        )
    except BaseException as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:400]}"
        topic_ledger_id = reservation_state.get("topic_ledger_reservation_id")
        if (
            topic_ledger_id
            and not reservation_state.get("finalized")
            and not reservation_state.get("external_published")
            and not reservation_state.get("external_unknown")
            and reservation_state.get("topic_stage") != "publishing"
        ):
            try:
                topic_ledger.cancel(
                    reservation_state["topic_ledger_spec"],
                    reservation_state["topic_ledger_corner"],
                    reservation_state["topic_ledger_topic"],
                    topic_ledger_id,
                    reason,
                    metadata=reservation_state.get("topic_ledger_metadata"),
                )
            except Exception as cleanup_exc:  # 元の制作失敗を隠さない
                _log(f"共通題材台帳の取消失敗: {cleanup_exc}")
        reservation_id = reservation_state.get("reservation_id")
        if (
            reservation_id
            and not reservation_state.get("finalized")
            and not reservation_state.get("external_published")
            and not reservation_state.get("external_unknown")
            and reservation_state.get("topic_stage") != "publishing"
        ):
            try:
                history.cancel_topic(
                    reservation_state["spec"],
                    reservation_state["corner"],
                    reservation_state["topic"],
                    reservation_id,
                    reason,
                    metadata=reservation_state.get("topic_metadata"),
                )
            except Exception as cleanup_exc:  # 元の制作失敗を隠さない
                _log(f"チャネル題材履歴の取消失敗: {cleanup_exc}")
        performance_application_id = reservation_state.get(
            "performance_application_id"
        )
        if (
            performance_application_id
            and not reservation_state.get("finalized")
            and not reservation_state.get("external_published")
            and not reservation_state.get("external_unknown")
            and reservation_state.get("topic_stage") != "publishing"
        ):
            try:
                history.cancel_performance_decision(
                    reservation_state["performance_spec"],
                    reservation_state["performance_corner"],
                    reservation_state["performance_decision_id"],
                    performance_application_id,
                    reason,
                )
            except Exception as cleanup_exc:  # 元の制作失敗を隠さない
                _log(f"実績仮説の取消失敗: {cleanup_exc}")
        raise


def _list_channels() -> list[dict]:
    rows: list[dict] = []
    for channel_id in channel.discover():
        try:
            spec = channel.load(channel_id)
            last = history.last_run(spec)
            last_summary = (
                {
                    key: last.get(key)
                    for key in ("ts", "corner", "title", "video_id", "duration_sec")
                    if key in last
                }
                if last
                else None
            )
            rows.append(
                {
                    "channel": spec.id,
                    "name": spec.name,
                    "last_run": last_summary,
                }
            )
        except Exception as exc:  # 壊れた1設定が他チャンネルの一覧を妨げない
            rows.append(
                {"channel": channel_id, "status": "error", "error": str(exc)}
            )
    return rows


def _run_all_channels(
    day: str,
    *,
    do_upload: bool,
    video_scenes: int,
) -> tuple[dict, int]:
    results: list[dict] = []
    for channel_id in channel.discover():
        try:
            spec = channel.load(channel_id)
            result = run(
                spec,
                day,
                None,
                do_upload=do_upload,
                video_scenes=video_scenes,
            )
            results.append(
                {"channel": channel_id, "status": "ok", "result": result}
            )
        except (
            history.TopicCooldownSkip,
            history.PerformanceEvalWindowSkip,
        ) as exc:
            results.append(
                {
                    "channel": channel_id,
                    "status": "skipped",
                    "reason": exc.reason,
                }
            )
        except topic_ledger.DailyUploadLimitSkip as exc:
            results.append(
                {
                    "channel": channel_id,
                    "status": "skipped",
                    "reason": exc.reason,
                }
            )
        except Exception as exc:  # 1チャンネル失敗でも残りを逐次実行する
            _log(f"channel={channel_id} ERROR: {exc}")
            results.append(
                {"channel": channel_id, "status": "error", "error": str(exc)}
            )
    succeeded = sum(item["status"] == "ok" for item in results)
    skipped = sum(item["status"] == "skipped" for item in results)
    failed = len(results) - succeeded - skipped
    summary = {
        "mode": "all_channels",
        "date": day,
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "channels": results,
    }
    # 従来どおり一部チャンネルが成功すればジョブ全体は成功扱い。
    # 全件cooldownスキップも意図した正常動作なのでexit 0にする。
    return summary, 0 if (succeeded or skipped) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="doci 日次生成")
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--channel", help="チャンネルID")
    target.add_argument(
        "--all-channels",
        action="store_true",
        help="全チャンネルを逐次実行（1件の失敗で他を止めない）",
    )
    target.add_argument(
        "--list-channels",
        action="store_true",
        help="チャンネル一覧と直近実行を表示",
    )
    target.add_argument(
        "--recover-publishing",
        metavar="RESERVATION_ID",
        help="外部結果を運用者が確認済みのpublishing予約を終端化",
    )
    ap.add_argument(
        "--corner",
        help="指定が無ければチャンネル履歴の前回と交互",
    )
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--no-upload", action="store_true", help="生成のみ（アップロードしない）")
    ap.add_argument("--video-scenes", type=int, default=config.MINIMAX_VIDEO_SCENES)
    ap.add_argument(
        "--recover-performance-application",
        metavar="APPLICATION_ID",
        help=(
            "外部結果を運用者が確認済みの実績適用（--channel必須）を終端化。"
            "投稿結果不明のまま残った予約を解消し、cornerの次実験を再度許可する"
        ),
    )
    ap.add_argument(
        "--recovery-status",
        choices=("cancelled", "published"),
        default="cancelled",
        help="publishing/実績適用復旧の終端状態（--recover-publishing/--recover-performance-application専用）",
    )
    ap.add_argument(
        "--recovery-video-id",
        help="published復旧時に外部で確認した動画ID",
    )
    ap.add_argument(
        "--recovery-reason",
        default="運用者が外部投稿の結果を確認し、未完了予約を復旧",
        help="publishing/実績適用復旧の監査理由",
    )
    args = ap.parse_args()
    if args.list_channels:
        print(json.dumps(_list_channels(), ensure_ascii=False, indent=2))
        return 0
    if args.recover_publishing:
        try:
            result = topic_ledger.recover_publishing(
                args.recover_publishing,
                status=args.recovery_status,
                video_id=args.recovery_video_id,
                reason=args.recovery_reason,
            )
        except Exception as exc:
            _log(f"publishing復旧失敗: {exc}")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.recover_performance_application:
        if not args.channel:
            ap.error("--recover-performance-application には --channel が必要です")
        try:
            spec = channel.load(args.channel)
            result = history.recover_performance_application(
                spec,
                args.recover_performance_application,
                status=args.recovery_status,
                video_id=args.recovery_video_id,
                reason=args.recovery_reason,
            )
        except Exception as exc:
            _log(f"実績適用復旧失敗: {exc}")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.all_channels:
        if args.corner:
            ap.error("--corner は --all-channels と同時に指定できません")
        result, exit_code = _run_all_channels(
            args.date,
            do_upload=not args.no_upload,
            video_scenes=args.video_scenes,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    try:
        spec = channel.load(args.channel or channel.default_channel())
        result = run(
            spec,
            args.date,
            args.corner,
            do_upload=not args.no_upload,
            video_scenes=args.video_scenes,
        )
    except (
        history.TopicCooldownSkip,
        history.PerformanceEvalWindowSkip,
    ) as exc:
        print(
            json.dumps(
                {
                    "channel": spec.id,
                    "status": "skipped",
                    "reason": exc.reason,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except topic_ledger.DailyUploadLimitSkip as exc:
        print(
            json.dumps(
                {
                    "channel": spec.id,
                    "status": "skipped",
                    "reason": exc.reason,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as e:
        _log(f"ERROR: {e}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
