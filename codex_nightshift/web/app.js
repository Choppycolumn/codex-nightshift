let state = { sessions: [] };
let query = "";
const drafts = new Map();

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast show${error ? " error" : ""}`;
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.message || "操作失败");
  return data;
}

function quotaCard(name, data) {
  const used = Math.max(0, Math.min(100, Number(data?.used ?? 0)));
  $(`${name}-value`).textContent = data?.used == null ? "--" : `${Math.round(used)}%`;
  $(`${name}-meter`).style.width = `${used}%`;
  $(`${name}-reset`).textContent = data ? `重置时间 ${data.reset}` : "暂无限额记录";
}

function updateDraft(sessionId, field, value) {
  const draft = drafts.get(sessionId) || {};
  draft[field] = value;
  drafts.set(sessionId, draft);
}

function clearDraft(sessionId, field = "") {
  if (!field) {
    drafts.delete(sessionId);
    return;
  }
  const draft = drafts.get(sessionId);
  if (!draft) return;
  delete draft[field];
  if (Object.keys(draft).length) {
    drafts.set(sessionId, draft);
  } else {
    drafts.delete(sessionId);
  }
}

function scheduleDraftFromTask(task) {
  return {
    enabled: task.querySelector(".schedule-enabled").checked,
    run_at: task.querySelector(".schedule-run-at").value,
    repeat: task.querySelector(".schedule-repeat").value,
    prompt: task.querySelector(".schedule-prompt").value,
  };
}

function isTaskFormActive() {
  const active = document.activeElement;
  return Boolean(
    active
    && active.closest
    && active.closest(".task")
    && active.matches("input, textarea, select")
  );
}

function render(options = {}) {
  quotaCard("primary", state.quota?.primary);
  quotaCard("secondary", state.quota?.secondary);
  $("adopted-count").textContent = state.adopted_count ?? 0;
  $("watcher-dot").classList.toggle("on", Boolean(state.watcher_running));
  $("watcher-label").textContent = state.watcher_running ? "后台守护正在运行" : "后台守护未运行";
  $("start-watcher").textContent = state.watcher_running ? "重新安装后台守护" : "启动后台守护";

  if (options.preserveTaskList && (isTaskFormActive() || drafts.size > 0)) {
    return;
  }

  const sessions = state.sessions.filter((item) => {
    const haystack = `${item.title} ${item.project} ${item.cwd} ${item.id}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  $("task-list").innerHTML = sessions.length ? sessions.map(taskHtml).join("") : '<div class="empty">没有匹配的任务</div>';
  bindTasks();
}

function taskHtml(item) {
  const draft = drafts.get(item.id) || {};
  const schedule = draft.scheduled_command || item.scheduled_command || {};
  const resumePrompt = draft.resume_prompt ?? item.resume_prompt;
  const interruptedLabel = item.confidence === "high" ? "异常中断" : "可能中断";
  const statusLabel = { active: "执行中", complete: "已完成", stopped: "已停止", interrupted: interruptedLabel }[item.state] || item.state;
  const lastResult = item.last_result ? `<span class="badge">上次续跑 ${esc(item.last_result)}</span>` : "";
  const pausedBadge = item.paused ? `<span class="badge interrupted">自动暂停：${esc(item.pause_reason || "续跑失败")}</span>` : "";
  const scheduleBadge = schedule.enabled ? `<span class="badge">计划 ${esc(schedule.run_at)}</span>` : "";
  return `
    <article class="task ${item.auto ? "is-auto" : ""}" data-id="${esc(item.id)}">
      <div>
        <h3 class="task-title" title="${esc(item.title)}">${esc(item.title)}</h3>
        <div class="task-meta">
          <span class="badge ${esc(item.state)}">${esc(statusLabel)}</span>
          <span>${esc(item.project)}</span>
          <span>${esc(item.last_active)}</span>
          <span>${esc(item.short_id)}</span>
          ${lastResult}
          ${pausedBadge}
          ${scheduleBadge}
        </div>
      </div>
      <div class="controls">
        <label class="mode">
          <span>自动执行条件</span>
          <select class="strict-mode" ${item.auto ? "" : "disabled"}>
            <option value="normal" ${item.strict ? "" : "selected"}>中断或停滞时</option>
            <option value="strict" ${item.strict ? "selected" : ""}>仅额度耗尽时</option>
          </select>
        </label>
        <label class="switch" title="${item.auto ? "自动续跑已开启" : "不自动执行"}">
          <input class="auto-toggle" type="checkbox" ${item.auto ? "checked" : ""}>
          <span class="slider"></span>
        </label>
      </div>
      <div class="prompt-row">
        <label>
          <span>恢复后的第一句指令</span>
          <textarea class="resume-prompt" rows="2" placeholder="留空则使用默认续跑指令" ${item.auto ? "" : "disabled"}>${esc(resumePrompt)}</textarea>
        </label>
        <button class="button save-prompt" ${item.auto ? "" : "disabled"}>保存指令</button>
      </div>
      <div class="schedule-row">
        <label class="schedule-enabled-label">
          <span>定时命令</span>
          <input class="schedule-enabled" type="checkbox" ${schedule.enabled ? "checked" : ""} ${item.auto ? "" : "disabled"}>
        </label>
        <label>
          <span>下达时间</span>
          <input class="schedule-run-at" type="datetime-local" value="${esc(schedule.run_at || "")}" ${item.auto ? "" : "disabled"}>
        </label>
        <label>
          <span>重复</span>
          <select class="schedule-repeat" ${item.auto ? "" : "disabled"}>
            <option value="once" ${(schedule.repeat || "once") === "once" ? "selected" : ""}>一次</option>
            <option value="hourly" ${schedule.repeat === "hourly" ? "selected" : ""}>每小时</option>
            <option value="daily" ${schedule.repeat === "daily" ? "selected" : ""}>每天</option>
          </select>
        </label>
        <label class="schedule-command-label">
          <span>到点下达的命令</span>
          <textarea class="schedule-prompt" rows="2" placeholder="例如：检查 CI 状态并继续修复失败项" ${item.auto ? "" : "disabled"}>${esc(schedule.prompt || "")}</textarea>
        </label>
        <button class="button save-schedule" ${item.auto ? "" : "disabled"}>保存计划</button>
      </div>
    </article>`;
}

function bindTasks() {
  document.querySelectorAll(".task").forEach((task) => {
    const sessionId = task.dataset.id;
    task.querySelector(".auto-toggle").addEventListener("change", async (event) => {
      const enabled = event.target.checked;
      const strict = task.querySelector(".strict-mode").value === "strict";
      event.target.disabled = true;
      try {
        const result = await api("/api/session/auto", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId, enabled, strict }),
        });
        toast(result.message);
        if (!enabled) clearDraft(sessionId);
        await refresh();
      } catch (error) {
        toast(error.message, true);
        event.target.checked = !enabled;
      } finally {
        event.target.disabled = false;
      }
    });
    task.querySelector(".strict-mode").addEventListener("change", async (event) => {
      const strict = event.target.value === "strict";
      try {
        const result = await api("/api/session/mode", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId, strict }),
        });
        toast(result.message);
        await refresh();
      } catch (error) {
        toast(error.message, true);
      }
    });
    task.querySelector(".resume-prompt").addEventListener("input", (event) => {
      updateDraft(sessionId, "resume_prompt", event.target.value);
    });
    task.querySelector(".save-prompt").addEventListener("click", async (event) => {
      const prompt = task.querySelector(".resume-prompt").value;
      event.target.disabled = true;
      try {
        const result = await api("/api/session/prompt", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId, prompt }),
        });
        toast(result.message);
        clearDraft(sessionId, "resume_prompt");
        await refresh();
      } catch (error) {
        toast(error.message, true);
      } finally {
        event.target.disabled = false;
      }
    });
    [".schedule-enabled", ".schedule-run-at", ".schedule-repeat", ".schedule-prompt"].forEach((selector) => {
      const el = task.querySelector(selector);
      const eventName = selector === ".schedule-prompt" || selector === ".schedule-run-at" ? "input" : "change";
      el.addEventListener(eventName, () => {
        updateDraft(sessionId, "scheduled_command", scheduleDraftFromTask(task));
      });
    });
    task.querySelector(".save-schedule").addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        const result = await api("/api/session/schedule", {
          method: "POST",
          body: JSON.stringify({
            session_id: sessionId,
            enabled: task.querySelector(".schedule-enabled").checked,
            run_at: task.querySelector(".schedule-run-at").value,
            repeat: task.querySelector(".schedule-repeat").value,
            prompt: task.querySelector(".schedule-prompt").value,
          }),
        });
        toast(result.message);
        clearDraft(sessionId, "scheduled_command");
        await refresh();
      } catch (error) {
        toast(error.message, true);
      } finally {
        event.target.disabled = false;
      }
    });
  });
}

async function refresh(options = {}) {
  try {
    state = await api("/api/state");
    render(options);
  } catch (error) {
    toast(error.message, true);
  }
}

async function background(action) {
  try {
    const result = await api("/api/background", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    toast(result.message);
    setTimeout(refresh, 700);
  } catch (error) {
    toast(error.message, true);
  }
}

$("search").addEventListener("input", (event) => { query = event.target.value; render(); });
$("refresh").addEventListener("click", refresh);
$("start-watcher").addEventListener("click", () => background("install_start"));
$("stop-watcher").addEventListener("click", () => background("stop"));
refresh();
setInterval(() => refresh({ preserveTaskList: true }), 10000);
