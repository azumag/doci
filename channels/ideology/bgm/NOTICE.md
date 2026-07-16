# BGM ライセンス

`internationale_piano.mp3` は以下から生成した、権利的にクリーンな音源です。

- **旋律**: 「インターナショナル」(The Internationale)。作曲 Pierre De Geyter (1848–1932)。
  旋律自体はパブリックドメイン（作曲者の没後70年以上経過）。
- **元データ**: Wikimedia Commons の PD MIDI
  `Internationale-piano-Bb.mid`
  https://commons.wikimedia.org/wiki/File:Internationale-piano-Bb.mid
  （PD-Internationale / PD-old）
- **演奏（レンダリング）**: 本リポジトリの `tools/make_bgm.py` が stdlib のみで合成。
  外部サウンドフォント・第三者録音は使用していないため、録音物の著作隣接権は発生しない。

したがって本 mp3 は YouTube 配信での BGM 利用に問題ありません。

別の音源（例: 実際のピアノ演奏）を使いたい場合は、`channels/ideology/bgm/` に音声ファイル
（mp3/ogg/wav）を置けば自動的にそちらが使われます（`doci/config.py: bgm_path()`）。

再生成:
```
python tools/make_bgm.py --midi internationale.mid --out channels/ideology/bgm/internationale_piano.mp3
```
