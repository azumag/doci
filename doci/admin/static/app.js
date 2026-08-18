"use strict";

const TOKEN = document.querySelector('meta[name="doci-admin-token"]').content;
const ENABLE_CODE_PROMPTS =
  document.querySelector('meta[name="doci-admin-enable-code-prompts"]').content === "1";

async function api(method, path, body) {
  const opts = { method, headers: { "X-Doci-Token": TOKEN } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let payload = {};
  try {
    payload = await res.json();
  } catch (e) {
    /* 空ボディ等 */
  }
  return { status: res.status, ok: res.ok, payload };
}

function msgEl(kind, text) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.textContent = text;
  return div;
}

function warningsEl(warnings) {
  if (!warnings || !warnings.length) return null;
  return msgEl("warn", "警告:\n" + warnings.map((w) => "- " + w).join("\n"));
}

async function confirmAndRetry(firstRes, retry) {
  if (firstRes.status === 409 && firstRes.payload.needs_confirmation) {
    const ok = window.confirm(
      "警告があります。続行しますか？\n\n" + (firstRes.payload.warnings || []).join("\n")
    );
    if (!ok) return firstRes;
    return await retry();
  }
  return firstRes;
}

const app = document.getElementById("app");
const routes = {
  env: renderEnv,
  channels: renderChannels,
  prompts: renderPrompts,
  "code-prompts": renderCodePrompts,
};

function currentRoute() {
  const h = location.hash.replace("#", "");
  return routes[h] ? h : "env";
}

function updateNav() {
  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.route === currentRoute());
  });
}

async function render() {
  updateNav();
  const route = currentRoute();
  app.innerHTML = "";
  if (route === "code-prompts" && !ENABLE_CODE_PROMPTS) {
    app.appendChild(
      msgEl("warn", "--enable-code-prompts 未指定のため、この機能は無効です。")
    );
    return;
  }
  try {
    await routes[route]();
  } catch (err) {
    app.appendChild(msgEl("error", "画面の描画に失敗しました: " + err));
  }
}

window.addEventListener("hashchange", render);

async function pollStatus() {
  const { ok, payload } = await api("GET", "/api/status");
  const banner = document.getElementById("banner");
  if (ok && payload.pipeline_running && payload.pipeline_running.length) {
    banner.textContent =
      "cron実行中: " +
      payload.pipeline_running.map((r) => `${r.run_name}(pid=${r.pid})`).join(", ") +
      " — 保存前に十分確認してください";
    banner.classList.add("show");
  } else {
    banner.classList.remove("show");
  }
}

// --- Env ---

function looksTrue(value) {
  return ["1", "true", "yes", "on"].includes((value || "").toLowerCase());
}

async function renderEnv() {
  const h2 = document.createElement("h2");
  h2.textContent = "Env (.env)";
  app.appendChild(h2);
  const loading = msgEl("", "読み込み中...");
  app.appendChild(loading);

  const { ok, payload } = await api("GET", "/api/env");
  loading.remove();
  if (!ok) {
    app.appendChild(msgEl("error", "読み込みに失敗しました"));
    return;
  }

  let baseFingerprint = payload.fingerprint;
  const pending = {};
  const enableSet = new Set();

  const filterInput = document.createElement("input");
  filterInput.type = "text";
  filterInput.placeholder = "キーで絞り込み...";
  filterInput.style.marginBottom = "8px";
  app.appendChild(filterInput);

  const table = document.createElement("table");
  table.innerHTML = "<thead><tr><th>キー</th><th>値</th><th>説明</th></tr></thead>";
  const tbody = document.createElement("tbody");
  table.appendChild(tbody);
  app.appendChild(table);

  const msgArea = document.createElement("div");
  app.appendChild(msgArea);

  const btnRow = document.createElement("div");
  btnRow.style.marginTop = "12px";
  const validateBtn = document.createElement("button");
  validateBtn.textContent = "検証";
  const saveBtn = document.createElement("button");
  saveBtn.textContent = "保存";
  saveBtn.className = "primary";
  saveBtn.style.marginLeft = "8px";
  btnRow.appendChild(validateBtn);
  btnRow.appendChild(saveBtn);
  app.appendChild(btnRow);

  function buildValueCell(entry) {
    const tdVal = document.createElement("td");
    if (entry.is_secret) {
      const span = document.createElement("span");
      span.className = "muted";
      span.textContent = entry.is_set ? `設定済み (fingerprint ${entry.fingerprint})` : "未設定";
      tdVal.appendChild(span);
      const replaceBtn = document.createElement("button");
      replaceBtn.textContent = "置換";
      replaceBtn.style.marginLeft = "8px";
      replaceBtn.onclick = () => {
        replaceBtn.remove();
        span.remove();
        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "新しい値";
        input.oninput = () => {
          pending[entry.key] = input.value;
        };
        tdVal.appendChild(input);
      };
      tdVal.appendChild(replaceBtn);
    } else if (entry.kind === "bool") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = looksTrue(entry.value);
      input.onchange = () => {
        pending[entry.key] = input.checked ? "1" : "0";
      };
      tdVal.appendChild(input);
    } else if (entry.choices && entry.choices.length) {
      const select = document.createElement("select");
      if (!entry.is_set) {
        const optNone = document.createElement("option");
        optNone.value = "";
        optNone.textContent = "(未設定)";
        select.appendChild(optNone);
      }
      entry.choices.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        if (c === entry.value) opt.selected = true;
        select.appendChild(opt);
      });
      select.onchange = () => {
        pending[entry.key] = select.value;
      };
      tdVal.appendChild(select);
    } else {
      const input = document.createElement("input");
      input.type = "text";
      input.value = entry.value || "";
      input.placeholder = entry.is_set ? "" : "(未設定)";
      input.oninput = () => {
        pending[entry.key] = input.value;
      };
      tdVal.appendChild(input);
    }
    if (!entry.enabled && entry.line_no !== null) {
      const label = document.createElement("label");
      label.style.marginLeft = "8px";
      label.className = "muted";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.onchange = () => {
        if (cb.checked) enableSet.add(entry.key);
        else enableSet.delete(entry.key);
      };
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" 有効化(コメント解除)"));
      tdVal.appendChild(label);
    }
    return tdVal;
  }

  payload.entries.forEach((entry) => {
    const tr = document.createElement("tr");
    tr.dataset.key = entry.key;
    const tdKey = document.createElement("td");
    const codeEl = document.createElement("code");
    codeEl.textContent = entry.key;
    tdKey.appendChild(codeEl);
    if (!entry.known) {
      const note = document.createElement("span");
      note.className = "muted";
      note.textContent = " (config.py未使用)";
      tdKey.appendChild(note);
    }
    tr.appendChild(tdKey);
    tr.appendChild(buildValueCell(entry));
    const tdDoc = document.createElement("td");
    tdDoc.className = "muted";
    tdDoc.textContent = (entry.doc || "").split("\n")[0] || "";
    tdDoc.title = entry.doc || "";
    tr.appendChild(tdDoc);
    tbody.appendChild(tr);
  });

  filterInput.oninput = () => {
    const q = filterInput.value.toLowerCase();
    tbody.querySelectorAll("tr").forEach((tr) => {
      tr.style.display = tr.dataset.key.toLowerCase().includes(q) ? "" : "none";
    });
  };

  validateBtn.onclick = async () => {
    const { payload: result } = await api("POST", "/api/env/validate", {
      changes: pending,
      enable: [...enableSet],
    });
    msgArea.innerHTML = "";
    if (result.ok) {
      msgArea.appendChild(
        msgEl("ok", "検証OK" + (result.channels ? ` (channels: ${result.channels.join(", ")})` : ""))
      );
    } else {
      if (result.error) msgArea.appendChild(msgEl("error", result.error));
      const w = warningsEl(result.warnings);
      if (w) msgArea.appendChild(w);
    }
  };

  saveBtn.onclick = async () => {
    if (Object.keys(pending).length === 0 && enableSet.size === 0) {
      msgArea.innerHTML = "";
      msgArea.appendChild(msgEl("warn", "変更がありません"));
      return;
    }
    const doSave = (confirmWarnings) =>
      api("POST", "/api/env/save", {
        changes: pending,
        enable: [...enableSet],
        confirm_warnings: confirmWarnings,
        base_fingerprint: baseFingerprint,
      });
    let res = await doSave(false);
    res = await confirmAndRetry(res, () => doSave(true));
    msgArea.innerHTML = "";
    if (res.ok) {
      msgArea.appendChild(msgEl("ok", "保存しました"));
      render();
    } else {
      if (res.payload.error) msgArea.appendChild(msgEl("error", res.payload.error));
      const w = warningsEl(res.payload.warnings);
      if (w) msgArea.appendChild(w);
    }
  };
}

// --- Channels ---

async function renderChannels() {
  const h2 = document.createElement("h2");
  h2.textContent = "Channels (channel.toml)";
  app.appendChild(h2);

  const { payload: list } = await api("GET", "/api/channels");
  const select = document.createElement("select");
  list.channels.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = `${c.id} (${c.name})`;
    select.appendChild(opt);
  });
  app.appendChild(select);

  const body = document.createElement("div");
  app.appendChild(body);

  async function loadChannel(cid) {
    body.innerHTML = "読み込み中...";
    const { payload } = await api("GET", `/api/channels/${encodeURIComponent(cid)}/toml`);
    body.innerHTML = "";

    const row = document.createElement("div");
    row.className = "row";
    const col1 = document.createElement("div");
    col1.className = "col";
    const textarea = document.createElement("textarea");
    textarea.rows = 28;
    textarea.value = payload.text;
    col1.appendChild(textarea);
    const msgArea = document.createElement("div");
    col1.appendChild(msgArea);
    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "8px";
    const validateBtn = document.createElement("button");
    validateBtn.textContent = "検証";
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.className = "primary";
    saveBtn.style.marginLeft = "8px";
    btnRow.appendChild(validateBtn);
    btnRow.appendChild(saveBtn);
    col1.appendChild(btnRow);

    const col2 = document.createElement("div");
    col2.className = "col";
    const previewLabel = document.createElement("div");
    previewLabel.className = "muted";
    previewLabel.textContent = "解決後プレビュー(読み取り専用):";
    col2.appendChild(previewLabel);
    const pre = document.createElement("pre");
    col2.appendChild(pre);

    row.appendChild(col1);
    row.appendChild(col2);
    body.appendChild(row);

    function renderPreview(validation) {
      pre.textContent = validation.ok
        ? JSON.stringify(validation.summary, null, 2)
        : "エラー:\n" + validation.error;
    }
    renderPreview(payload.validation);

    let baseFingerprint = payload.fingerprint;

    validateBtn.onclick = async () => {
      const { payload: v } = await api("POST", `/api/channels/${encodeURIComponent(cid)}/validate`, {
        text: textarea.value,
      });
      renderPreview(v);
      msgArea.innerHTML = "";
      if (!v.ok) msgArea.appendChild(msgEl("error", v.error));
      const w = warningsEl(v.warnings);
      if (w) msgArea.appendChild(w);
    };

    saveBtn.onclick = async () => {
      const doSave = (confirmWarnings) =>
        api("POST", `/api/channels/${encodeURIComponent(cid)}/save`, {
          text: textarea.value,
          confirm_warnings: confirmWarnings,
          base_fingerprint: baseFingerprint,
        });
      let res = await doSave(false);
      res = await confirmAndRetry(res, () => doSave(true));
      msgArea.innerHTML = "";
      if (res.ok) {
        msgArea.appendChild(msgEl("ok", "保存しました"));
        baseFingerprint = res.payload.fingerprint;
        renderPreview({ ok: true, summary: res.payload.summary });
      } else {
        if (res.payload.error) msgArea.appendChild(msgEl("error", res.payload.error));
        const w = warningsEl(res.payload.warnings);
        if (w) msgArea.appendChild(w);
      }
    };
  }

  select.onchange = () => loadChannel(select.value);
  if (list.channels.length) loadChannel(list.channels[0].id);
}

// --- Prompts (Markdown) ---

async function renderPrompts() {
  const h2 = document.createElement("h2");
  h2.textContent = "Prompts (Markdown)";
  app.appendChild(h2);

  const { payload: list } = await api("GET", "/api/prompts");
  const row = document.createElement("div");
  row.className = "row";
  const listCol = document.createElement("div");
  listCol.className = "col";
  listCol.style.maxWidth = "260px";
  const editCol = document.createElement("div");
  editCol.className = "col";
  row.appendChild(listCol);
  row.appendChild(editCol);
  app.appendChild(row);

  async function loadSlot(slot) {
    editCol.innerHTML = "読み込み中...";
    const { payload } = await api("GET", `/api/prompts/${encodeURIComponent(slot)}`);
    editCol.innerHTML = "";
    if (payload.required_tokens && payload.required_tokens.length) {
      const info = document.createElement("div");
      info.className = "muted";
      info.textContent = "必須トークン: " + payload.required_tokens.join(" / ");
      editCol.appendChild(info);
    }
    const textarea = document.createElement("textarea");
    textarea.rows = 26;
    textarea.value = payload.text;
    editCol.appendChild(textarea);
    const msgArea = document.createElement("div");
    editCol.appendChild(msgArea);
    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "8px";
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.className = "primary";
    btnRow.appendChild(saveBtn);
    editCol.appendChild(btnRow);
    let baseFingerprint = payload.fingerprint;

    saveBtn.onclick = async () => {
      const doSave = (confirmWarnings) =>
        api("POST", `/api/prompts/${encodeURIComponent(slot)}/save`, {
          text: textarea.value,
          confirm_warnings: confirmWarnings,
          base_fingerprint: baseFingerprint,
        });
      let res = await doSave(false);
      res = await confirmAndRetry(res, () => doSave(true));
      msgArea.innerHTML = "";
      if (res.ok) {
        msgArea.appendChild(msgEl("ok", "保存しました"));
        baseFingerprint = res.payload.fingerprint;
      } else {
        if (res.payload.error) msgArea.appendChild(msgEl("error", res.payload.error));
        const w = warningsEl(res.payload.warnings);
        if (w) msgArea.appendChild(w);
      }
    };
  }

  list.prompts.forEach((p, idx) => {
    const item = document.createElement("div");
    item.className = "list-item" + (idx === 0 ? " selected" : "");
    const title = document.createElement("div");
    title.textContent = p.slot + (p.exists ? "" : " (未作成)");
    item.appendChild(title);
    if (p.used_by.length) {
      const m = document.createElement("div");
      m.className = "muted";
      m.textContent = "共有: " + p.used_by.join(", ");
      item.appendChild(m);
    }
    item.onclick = () => {
      listCol.querySelectorAll(".list-item").forEach((el) => el.classList.remove("selected"));
      item.classList.add("selected");
      loadSlot(p.slot);
    };
    listCol.appendChild(item);
  });

  if (list.prompts.length) loadSlot(list.prompts[0].slot);
}

// --- Code prompts (gated) ---

async function renderCodePrompts() {
  const h2 = document.createElement("h2");
  h2.textContent = "Code prompts";
  app.appendChild(h2);
  app.appendChild(
    msgEl(
      "warn",
      "doci/*.py 内の文字列定数を直接書き換えます。AGENTS.mdのレビューフローを経由しません。"
    )
  );

  const { payload: list } = await api("GET", "/api/code-prompts");
  const row = document.createElement("div");
  row.className = "row";
  const listCol = document.createElement("div");
  listCol.className = "col";
  listCol.style.maxWidth = "280px";
  const editCol = document.createElement("div");
  editCol.className = "col";
  row.appendChild(listCol);
  row.appendChild(editCol);
  app.appendChild(row);

  async function loadConst(entry) {
    editCol.innerHTML = "読み込み中...";
    const { payload } = await api("GET", `/api/code-prompts/${encodeURIComponent(entry.id)}`);
    editCol.innerHTML = "";

    const fieldsDiv = document.createElement("div");
    entry.fields.forEach((f) => {
      const span = document.createElement("span");
      span.className = "chip ok";
      span.textContent = f;
      fieldsDiv.appendChild(span);
    });
    editCol.appendChild(fieldsDiv);
    if (entry.guarded_by.length) {
      editCol.appendChild(msgEl("warn", "この定数をアサートするテスト: " + entry.guarded_by.join(", ")));
    }

    const textarea = document.createElement("textarea");
    textarea.rows = 22;
    textarea.value = payload.text;
    editCol.appendChild(textarea);
    const msgArea = document.createElement("div");
    editCol.appendChild(msgArea);
    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "8px";
    const validateBtn = document.createElement("button");
    validateBtn.textContent = "検証";
    const saveBtn = document.createElement("button");
    saveBtn.textContent = "保存";
    saveBtn.className = "primary";
    saveBtn.style.marginLeft = "8px";
    btnRow.appendChild(validateBtn);
    btnRow.appendChild(saveBtn);
    editCol.appendChild(btnRow);
    let baseFingerprint = payload.fingerprint;

    validateBtn.onclick = async () => {
      const { payload: v } = await api(
        "POST",
        `/api/code-prompts/${encodeURIComponent(entry.id)}/validate`,
        { text: textarea.value }
      );
      msgArea.innerHTML = "";
      (v.errors || []).forEach((e) => msgArea.appendChild(msgEl("error", e)));
      const w = warningsEl(v.warnings);
      if (w) msgArea.appendChild(w);
      if (v.ok) {
        msgArea.appendChild(msgEl("ok", "検証OK（.format()ドライラン成功）"));
        const pre = document.createElement("pre");
        pre.textContent = v.format_preview;
        msgArea.appendChild(pre);
      }
    };

    saveBtn.onclick = async () => {
      const doSave = (confirmWarnings) =>
        api("POST", `/api/code-prompts/${encodeURIComponent(entry.id)}/save`, {
          text: textarea.value,
          confirm_warnings: confirmWarnings,
          base_fingerprint: baseFingerprint,
        });
      let res = await doSave(false);
      res = await confirmAndRetry(res, () => doSave(true));
      msgArea.innerHTML = "";
      if (res.ok) {
        msgArea.appendChild(msgEl("ok", "保存しました（バックアップ済み）"));
        baseFingerprint = res.payload.fingerprint;
        if (res.payload.test_result) {
          const tr = res.payload.test_result;
          msgArea.appendChild(
            msgEl(tr.ok ? "ok" : "error", `guarded testsの実行結果: ${tr.ok ? "成功" : "失敗"}\n${tr.output || ""}`)
          );
        }
      } else {
        (res.payload.errors || []).forEach((e) => msgArea.appendChild(msgEl("error", e)));
        if (res.payload.error) msgArea.appendChild(msgEl("error", res.payload.error));
        const w = warningsEl(res.payload.warnings);
        if (w) msgArea.appendChild(w);
      }
    };
  }

  list.code_prompts.forEach((entry, idx) => {
    const item = document.createElement("div");
    item.className = "list-item" + (idx === 0 ? " selected" : "");
    const idEl = document.createElement("div");
    idEl.textContent = entry.id;
    const descEl = document.createElement("div");
    descEl.className = "muted";
    descEl.textContent = entry.description;
    item.appendChild(idEl);
    item.appendChild(descEl);
    item.onclick = () => {
      listCol.querySelectorAll(".list-item").forEach((el) => el.classList.remove("selected"));
      item.classList.add("selected");
      loadConst(entry);
    };
    listCol.appendChild(item);
  });

  if (list.code_prompts.length) loadConst(list.code_prompts[0]);
}

// --- init ---

if (!ENABLE_CODE_PROMPTS) {
  const navEl = document.getElementById("nav-code-prompts");
  if (navEl) navEl.style.display = "none";
}
render();
setInterval(pollStatus, 15000);
pollStatus();
