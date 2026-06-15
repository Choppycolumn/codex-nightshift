let state = { sessions: [] };
let query = "";

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

function render() {
  quotaCard("primary", state.quota?.primary);
  quotaCard("secondary", state.quota?.secondary);
  $("adopted-count").textContent = state.adopted_count ?? 0;
  $("watcher-dot").classList.toggle("on", Boolean(state.watcher_running));
  $("watcher-label").textContent = state.watcher_running ? "后台守护正在运行" : "后台守护未运行";
  $("start-watcher").textContent = state.watcher_running ? "重新安装后台守护" : "启动后台守护";

  const sessions = state.sessions.filter((item) => {
    const haystack = `${item.title} ${item.project} ${item.cwd} ${item.id}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  $("task-list").innerHTML = sessions.length ? sessions.map(taskHtml).join("") : '<div class="empty">没有匹配的任务</div>';
  bindTasks();
}

function taskHtml(item) {
  const interruptedLabel = item.confidence === "high" ? "异常中断" : "可能中断";
  const statusLabel = { active: "执行中", complete: "已完成", stopped: "已停止", interrupted: interruptedLabel }[item.state] || item.state;
  const lastResult = item.last_result ? `<span class="badge">上次续跑 ${esc(item.last_result)}</span>` : "";
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
          <textarea class="resume-prompt" maxlength="4000" rows="2" placeholder="留空则使用默认续跑指令" ${item.auto ? "" : "disabled"}>${esc(item.resume_prompt)}</textarea>
        </label>
        <button class="button save-prompt" ${item.auto ? "" : "disabled"}>保存指令</button>
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
    task.querySelector(".save-prompt").addEventListener("click", async (event) => {
      const prompt = task.querySelector(".resume-prompt").value;
      event.target.disabled = true;
      try {
        const result = await api("/api/session/prompt", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId, prompt }),
        });
        toast(result.message);
        await refresh();
      } catch (error) {
        toast(error.message, true);
      } finally {
        event.target.disabled = false;
      }
    });
  });
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
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
setInterval(refresh, 10000);
