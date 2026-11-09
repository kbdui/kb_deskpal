"use strict";

const root = document.querySelector("#deskpal-studio");
const main = root.querySelector("#ds-main");
const overlay = root.querySelector("#ds-overlay");
const toastBox = root.querySelector("#ds-toast");
const paths = {
    orbit:'<circle cx="12" cy="12" r="3"/><ellipse cx="12" cy="12" rx="11" ry="5" transform="rotate(-42 12 12)"/>',
    "messages-square":'<path d="M14 3H4a1 1 0 0 0-1 1v10l4-3h7a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1ZM9 15v2h9l3 3V9h-3"/>',
    "calendar-days":'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18M8 15h2m4 0h2"/>',
    "book-open":'<path d="M12 6c-3-3-7-3-10-2v15c4-1 7-1 10 1 3-2 6-2 10-1V4c-3-1-7-1-10 2v14"/>',
    blocks:'<path d="M4 4h7v7H4zM4 14h7v7H4zM14 14h7v7h-7zM14 6l4-4 4 4-4 4z"/>',
    "settings-2":'<path d="M3 6h9m4 0h5M3 12h3m4 0h11M3 18h11m4 0h3"/><circle cx="14" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
    "chevron-down":'<path d="m6 9 6 6 6-6"/>', "chevron-right":'<path d="m9 6 6 6-6 6"/>',
    "arrow-up":'<path d="M12 19V5m-6 6 6-6 6 6"/>', "arrow-down":'<path d="M12 5v14m-6-6 6 6 6-6"/>',
    "arrow-up-right":'<path d="M7 17 17 7M7 7h10v10"/>',
    "panel-right":'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    "folder-open":'<path d="M3 19V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2H7l-4 8h15l3-8"/>',
    "scan-line":'<path d="M4 9V4h5m6 0h5v5m0 6v5h-5m-6 0H4v-5m0-3h16"/>',
    sparkles:'<path d="m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5Z"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
    check:'<path d="m5 12 4 4L19 6"/>', plus:'<path d="M12 5v14M5 12h14"/>',
    x:'<path d="m6 6 12 12M18 6 6 18"/>', download:'<path d="M12 3v12m-5-5 5 5 5-5M4 17v3h16v-3"/>',
    cat:'<path d="M4 14V5l5 3h6l5-3v9c0 5-4 7-8 7s-8-2-8-7Zm4 0h.1m7.9 0h.1M10 17l2 1 2-1"/>',
    dog:'<path d="M7 6C3 6 2 12 4 15l3-1m10-8c4 0 5 6 3 9l-3-1M7 6q5-4 10 0v10q0 5-5 5t-5-5Zm2 6h.1m5.9 0h.1M10 16l2 1 2-1"/>'
};
const icon = name => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (paths[name] || paths.blocks) + '</svg>';
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const attr = esc;
const settings = {role:"角色与称呼",model:"模型连接",appearance:"桌宠与外观",notifications:"通知",data:"数据与隐私"};
const statusNames = {
    Created:"排队中", Planning:"正在规划", Running:"进行中", Waiting:"待确认",
    NeedsReview:"待复核", AwaitingInput:"待补充", Paused:"已暂停", Completed:"已完成",
    Failed:"未完成", Cancelled:"已取消", Uncertain:"结果待核实", Succeeded:"已完成",
    due:"已到期", upcoming:"即将到来", pending:"等待中", scheduled:"已安排",
    accepted:"已采用", candidate:"待确认", disputed:"待确认"
};
const origins = {user_message:"来自聊天", tool_result:"来自执行结果", manual:"手动保存", conversation:"来自聊天"};
const state = {
    mode:"main", page:"chat", setting:"role", planTab:"upcoming", memoryTab:"facts", pluginTab:"catalog",
    planIndex:0, memoryIndex:0, pluginIndex:0, group:"all", offsets:{plans:0,memory:0,tools:0},
    query:"", openGroups:new Set(), execution:false, task:"", menu:false, chat:{},
    prefs:{}, images:{}, data:{}, draftCard:null, lifecycle:"Initializing"
};
const $ = selector => root.querySelector(selector);
const btn = (text, action, primary=false, symbol="") =>
    '<button type="button" class="ds-btn '+(primary?"ds-primary":"")+'" data-action="'+attr(action)+'">'+(symbol?icon(symbol):"")+esc(text)+'</button>';
const tag = (label, tone="accent") => '<span class="ds-pill ds-'+tone+'">'+esc(label)+'</span>';
const pair = (name,value) => '<div class="ds-meta-pair"><span>'+esc(name)+'</span><span>'+esc(value)+'</span></div>';
const empty = text => '<div class="ds-empty">'+esc(text)+'</div>';
const header = (title,sub="") => '<header class="ds-header"><div><h1>'+esc(title)+'</h1>'+
    (sub?'<p class="ds-muted">'+esc(sub)+'</p>':"")+'</div></header>';
const tabs = (values, field) => '<div class="ds-tabs" role="tablist">'+values.map(([key,name]) =>
    '<button type="button" role="tab" aria-selected="'+(state[field]===key)+'" class="'+(state[field]===key?"active":"")+
    '" data-tab="'+field+'" data-value="'+key+'">'+esc(name)+'</button>').join("")+'</div>';
const buttonRow = content => '<div class="ds-detail-footer">'+content+'</div>';
const option = (value,label,active) => '<option value="'+attr(value)+'"'+(active?" selected":"")+'>'+esc(label)+'</option>';
const friendly = value => {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {month:"long",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false}).format(date);
};
const longTime = value => {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN",{hour12:false});
};
const safeImage = value => /^data:image\/png;base64,[a-zA-Z0-9+/=]+$/.test(value || "") ? value : "";
const image = (key,fallback) => safeImage(state.images[key]) ?
    '<img src="'+attr(state.images[key])+'" alt="">' : icon(fallback);
const avatar = (key="role",large=false) => '<span class="ds-avatar '+(key==="user"?"ds-user ":"")+(large?"ds-large":"")+'">'+image(key,key==="user"?"dog":"cat")+'</span>';
const sender = who => who==="user" ? state.prefs.user_display_name || "你" : state.prefs.character_display_name || "Deskpal";
const statusText = value => statusNames[value] || value || "待更新";
let port;
let toastTimer;
let searchTimer;
let composing=false;
let queuedRender=null;

function send(name, args={}) {
    if (port) port.send(name, JSON.stringify(args));
}

function toast(text) {
    toastBox.textContent = text;
    toastBox.hidden = !text;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastBox.hidden = true; }, 4200);
}

function modal(title,body) {
    overlay.innerHTML = '<section class="ds-modal" role="dialog" aria-modal="true" aria-label="'+attr(title)+'">'+
        '<div class="ds-modal-header"><h2>'+esc(title)+'</h2><button type="button" class="ds-icon" data-action="close-modal" aria-label="关闭">'+icon("x")+'</button></div>'+body+'</section>';
    overlay.hidden = false;
    overlay.querySelector("input, select, textarea, button")?.focus();
}

function closeModal() {
    if(overlay.querySelector(".ds-preview-image")) send("asset-discard");
    overlay.hidden = true;
    overlay.replaceChildren();
}

function field(name,label,value="",type="text",extra="") {
    return '<label class="ds-field">'+esc(label)+'<input class="ds-input" name="'+attr(name)+'" type="'+type+
        '" value="'+attr(value)+'" '+extra+'></label>';
}

function form(name, body, submit) {
    return '<form data-form="'+name+'">'+body+'<div class="ds-detail-footer"><button type="submit" class="ds-btn ds-primary">'+esc(submit)+'</button></div></form>';
}

function structured(value,depth=0) {
    if (value === undefined || value === null) return "";
    if (typeof value !== "object") return esc(value);
    if (depth > 8) return '<pre class="ds-technical">'+esc(JSON.stringify(value,null,2))+'</pre>';
    const labels = {path:"文件",destination:"目标位置",source:"来源",title:"名称",content:"内容",start_at:"开始时间",
        end_at:"结束时间",duration_seconds:"时长（秒）",due_at:"到期时间",id:"标识",success:"是否成功",error:"错误"};
    return Object.entries(value).map(([key,data]) => {
        if (data === null) return "";
        return '<div class="ds-meta-pair"><span>'+esc(labels[key] || key)+'</span><span>'+
            (/_at$/.test(key)&&typeof data==="string"?esc(longTime(data)):structured(data,depth+1))+'</span></div>';
    }).join("");
}

function message(who,text,extra="") {
    const mine = who==="user";
    const head = mine ? esc(sender(who)) + avatar("user") : avatar() + esc(sender(who));
    return '<div class="ds-message '+(mine?"ds-me":"")+'"><div class="ds-message-head">'+head+
        '</div><div class="ds-message-text">'+esc(text)+extra+'</div></div>';
}

function sessionMenu() {
    const chat = state.chat;
    const items = (chat.sessions || []).map(row =>
        '<button type="button" data-session="'+attr(row.id)+'">'+esc(row.title)+
        (row.id===chat.default?tag("固定"):row.id===chat.id?icon("check"):"")+'</button>').join("");
    return '<div class="ds-session-wrap"><button type="button" class="ds-session-button" data-action="session-menu" aria-expanded="'+state.menu+'">'+
        esc(chat.title || "日常聊天")+icon("chevron-down")+'</button><div class="ds-menu" '+(state.menu?"":"hidden")+
        '>'+items+'<hr>'+ (chat.moreSessions?btn("更多会话","more-sessions"):"")+
        btn(chat.archived?"当前会话":"归档会话","archived-sessions")+
        btn("新建专项会话","new-session",false,"plus")+
        (chat.id && chat.id!==chat.default?btn("管理当前会话","manage-session"):"")+'</div></div>';
}

function composer() {
    const chat = state.chat;
    return '<div class="ds-compose-wrap"><form class="ds-compose" data-form="message"><textarea rows="2" maxlength="20000" name="text" aria-label="聊天消息" placeholder="'+
        (chat.working?"正在处理，可补充你的要求。":"想到什么，就说给我听。")+'" '+(chat.available?"":"disabled")+'>'+esc(chat.draft)+
        '</textarea><div class="ds-compose-meta"><span>Enter 发送 · Shift + Enter 换行</span><button class="ds-send" type="submit" aria-label="发送消息" '+
        (chat.canSend?"":"disabled")+'>'+icon("arrow-up")+'</button></div></form></div>';
}

const actions = {confirm:"确认并继续",cancel:"取消请求",task_pause:"暂停",task_continue:"继续",resume:"继续执行",
    review_uncertain:"核实结果",task_answer:"补充信息",task_append_requirement:"追加要求"};
function taskCard(id) {
    const item = (state.chat.reviews || {})[id];
    if (!item) return '<div class="ds-task"><p class="ds-muted">正在读取任务状态…</p></div>';
    const {review:r,grants,pending} = item;
    const buttons = (r.available_actions || []).filter(action => actions[action]);
    if(r.status==="AwaitingInput") buttons.push("task_answer");
    else if(["Paused","Created","Planning","Running","Waiting"].includes(r.status)) buttons.push("task_append_requirement");
    const high = r.missing_permissions?.high_risk_tools || [];
    const steps = r.steps || [];
    const confirming = buttons.includes("confirm");
    const detail = confirming ? (r.workspace?'<details><summary>执行位置</summary><p class="ds-technical">'+esc(r.workspace)+'</p></details>':"")+
        '<ol>'+steps.map(step => '<li><strong>'+esc(step.display_name || step.tool_name)+'</strong>'+
        structured(step.arguments)+'</li>').join("")+'</ol>' : "";
    const grantHtml = confirming ? high.map(key => '<label><input type="checkbox" data-grant="'+attr(key)+'" data-task="'+attr(id)+'" '+
        (grants.includes(key)?"checked":"")+(pending?" disabled":"")+'> 允许 '+esc(steps.find(step=>step.tool_name===key)?.display_name || key)+'</label>').join("") : "";
    const operationButtons = buttons.map(action => '<button type="button" class="ds-btn '+(action==="confirm"?"ds-primary":"")+
        '" data-task="'+attr(id)+'" data-task-action="'+action+'" '+(pending || action==="confirm"&&high.some(key=>!grants.includes(key))?"disabled":"")+
        '>'+actions[action]+'</button>').join("");
    return '<div class="ds-task" data-task-card="'+attr(id)+'"><div class="ds-row ds-between"><h3>'+esc(r.title || "任务")+'</h3>'+tag(statusText(r.status),confirming?"warn":"accent")+
        '</div>'+ (r.blocked_reason?'<p class="ds-error">'+esc(r.blocked_reason)+'</p>':"")+detail+grantHtml+
        (operationButtons || state.mode!=="pet"&&steps.length ? buttonRow(operationButtons+(state.mode==="pet"||!steps.length?"":'<button type="button" class="ds-btn" data-record="'+attr(id)+'">执行记录</button>')):"")+'</div>';
}

function execution() {
    if (!state.execution || state.mode==="pet") return "";
    const r = state.chat.reviews?.[state.task]?.review;
    const records = state.chat.reviews?.[state.task]?.records || [];
    const steps = records.map(({step,result,attempt}) => {
        const name = step?.display_name || step?.tool_name || result?.tool_name || "执行记录";
        const outcome = result ? (result.success ? "已完成" : "未完成") :
            attempt ? statusText(attempt.status) : "尚无执行结果";
        return '<div class="ds-step"><h3>'+esc(name)+'</h3><p>'+esc(outcome)+'</p>'+
            (result?.error?'<p class="ds-error">'+esc(result.error)+'</p>':"")+
            (result?.output!=null?'<div class="ds-muted">'+structured(result.output)+'</div>':"")+'</div>';
    }).join("");
    return '<aside class="ds-execution" aria-label="执行记录"><div class="ds-row ds-between ds-execution-title"><h3>执行记录</h3>'+
        '<button type="button" class="ds-icon" data-action="execution" aria-label="关闭执行记录">'+icon("x")+'</button></div>'+
        (r ? tag(statusText(r.status),r.status==="Completed"?"success":"accent")+'<h3 style="margin:15px 0 20px">'+esc(r.title)+'</h3>'+
            (steps || empty("这项任务没有工具步骤。"))+
            '<details><summary>技术详情</summary><div class="ds-technical">'+structured(r.task?.result)+'</div></details>' :
            empty(state.task?"正在读取执行记录…":"这段会话还没有执行记录。"))+'</aside>';
}

function scheduleCard(event) {
    const time = event.start_at || event.due_at;
    const date = time && new Date(time);
    const valid = date && !Number.isNaN(date.valueOf());
    return '<button type="button" class="ds-inline-event" data-event="'+attr(event.id)+'"><span class="ds-event-date">'+
        (valid?date.getMonth()+1+" 月<strong>"+date.getDate()+"</strong>":icon("calendar-days"))+
        '</span><span class="ds-grow"><strong>'+esc(event.title)+'</strong><span class="ds-muted" style="display:block">'+esc(friendly(time))+
        '</span></span>'+icon("chevron-right")+'</button>';
}

function chatPage() {
    const chat = state.chat;
    let lastDay = "";
    const rows = (chat.items || []).map(item => {
        if (item.visible===false) return "";
        if (item.kind==="task_card") return taskCard(item.task_id);
        if (item.redacted_at || item.redacted || item.scrubbed) return '<p class="ds-muted">这条记录已清理。</p>';
        if (!item.body) return "";
        const stamp = item.created_at || item.recorded_at;
        const day = stamp ? new Date(stamp).toLocaleDateString("zh-CN") : "";
        const separator = day && day!==lastDay ? '<div class="ds-date">'+esc(day)+'</div>' : "";
        if (day) lastDay = day;
        const extra = (state.mode==="pet"?[]:chat.events?.[item.task_id] || []).map(scheduleCard).join("");
        const more = item.body_truncated?'<button type="button" class="ds-link" data-message="'+attr(item.id)+'" data-offset="'+attr(item.body_offset || item.body.length)+'">继续阅读</button>':"";
        return separator+message(item.kind==="user_message" || item.kind==="user"?"user":"assistant",item.body,extra+more);
    }).join("");
    const pending = (chat.submissions || []).map(item=>message("user",item.request,
        '<div class="ds-muted">'+esc(item.state)+' <button type="button" class="ds-link" data-retry="'+attr(item.command_id)+'">'+(item.retryable?"使用原命令重试":"核查受理结果")+'</button></div>')).join("");
    const detailButton = state.mode==="pet" ? btn("","expand-chat",false,"arrow-up-right") : btn("执行记录","execution",false,"panel-right");
    return '<header class="ds-header"><div class="ds-row">'+
        (state.mode==="pet"?avatar(): '<button type="button" class="ds-icon" data-action="pet" aria-label="打开桌宠小窗">'+avatar("role",true)+'</button>')+
        '<div>'+sessionMenu()+(state.mode==="pet"?"":'<div class="ds-muted">'+(chat.id===chat.default?"长期会话":"专项会话")+" · 与 "+esc(sender("assistant"))+'</div>')+
        '</div></div><div class="ds-header-actions">'+detailButton+'</div></header>'+
        (state.lifecycle!=="Ready"||!chat.modelReady?'<div class="ds-status">'+esc(state.status || (state.lifecycle==="Ready"?"可浏览本地记录，请在设置中配置聊天模型。":"正在准备工作室…"))+'</div>':"")+
        '<div class="ds-chat-layout"><div class="ds-chat-center"><div class="ds-thread" id="ds-thread">'+
        (chat.more?btn("加载更早记录","older"):"")+
        (rows || pending ? rows+pending : empty(chat.available?"从一句话开始吧。":"正在读取会话…"))+'</div>'+
        '<button type="button" class="ds-bottom" data-action="bottom" aria-label="回到最新消息" hidden>'+icon("arrow-down")+'</button>'+
        composer()+'</div>'+execution()+'</div>';
}

function listButton(kind,index,selected,title,sub,marker="",symbol="") {
    return '<button type="button" class="ds-list-item '+(selected?"active":"")+'" data-'+kind+'="'+index+'" aria-pressed="'+selected+'">'+
        (symbol?icon(symbol):"")+'<span class="ds-grow"><h3>'+esc(title)+'</h3>'+(sub?'<p>'+esc(sub)+'</p>':"")+'</span>'+
        (marker||icon("chevron-right"))+'</button>';
}

function pagination(key,items) {
    const offset = state.offsets[key];
    return offset || items.has_more ? buttonRow((offset?btn("上一页","prev-"+key):"")+(items.has_more?btn("下一页","next-"+key):"")) : "";
}

function plansPage() {
    const result = state.data.plans || {};
    const rows = result.items || [];
    const selected = rows[state.planIndex] || rows[0];
    const isSchedule = state.planTab==="upcoming";
    const caption = {upcoming:"接下来的几天",ongoing:"需要继续处理",completed:"最近完成"}[state.planTab];
    const title = row => row.title || row.request || row.label || "未命名事项";
    const time = row => friendly(row.start_at || row.due_at || row.updated_at || row.created_at);
    const kind = row => isSchedule ? (row.source==="timer"?"计时器":"日程") : statusText(row.status);
    const detail = selected ? tag(kind(selected),state.planTab==="ongoing"?"warn":"accent")+
        '<h2>'+esc(title(selected))+'</h2><p>'+esc(selected.description || selected.note || selected.result?.final_response || "")+'</p>'+
        pair(isSchedule?"时间":"状态",isSchedule?time(selected):statusText(selected.status))+
        (isSchedule&&selected.reminder_at?pair("提醒时间",friendly(selected.reminder_at)):"")+
        (selected.end_at?pair("结束",friendly(selected.end_at)):"")+
        buttonRow(btn(isSchedule?"前往聊天处理":state.planTab==="completed"?"查看来源会话":"前往聊天处理","plan-chat",false,"messages-square")) :
        empty(result._error?"读取安排失败。":state.loading==="plans"?"正在读取安排…":"这里还没有安排。");
    return header("安排","查看日程和任务进度。需要新增、改期或取消时，在聊天中提出。")+
        '<div class="ds-page-body">'+tabs([["upcoming","即将到来"],["ongoing","进行中的任务"],["completed","已完成"]],"planTab")+
        '<div class="ds-split"><section><div class="ds-section-label">'+caption+'</div><div class="ds-list">'+
        rows.map((row,i)=>listButton("plan",i,row===selected,title(row),time(row),tag(kind(row)),isSchedule?"calendar-days":"check")).join("")+
        '</div>'+pagination("plans",result)+'</section><aside class="ds-detail">'+detail+'</aside></div></div>';
}

function memoryPage() {
    const result = state.data.memory || {};
    const rows = result.items || [];
    const selected = rows[state.memoryIndex] || rows[0];
    const body = row => row?.draft?.content || row?.content || "";
    const title = row => body(row).length>52?body(row).slice(0,52)+"…":body(row) || "记忆";
    const kind = {facts:"事实与偏好",episodes:"共同经历",buffer:"待确认的理解"}[state.memoryTab];
    const detail = selected ? tag(kind,state.memoryTab==="buffer"?"warn":"accent")+'<h2>'+esc(title(selected))+'</h2><p>'+
        esc(body(selected))+'</p>'+(selected.draft?.reason?'<blockquote>'+esc(selected.draft.reason)+'</blockquote>':"")+
        (selected.draft?.uncertainty?pair("尚待确认",selected.draft.uncertainty):"")+
        pair("来源",origins[selected.source?.origin] || "学习记录")+pair("最近更新",friendly(selected.recorded_at))+
        buttonRow(selected.source?.session_id || selected.draft?.quote || selected.source?.content ? btn("查看交流来源","memory-source",false,"messages-square"):"") :
        empty(result._error?"读取记忆失败。":state.loading==="memory"?"正在查找…":state.query?"没有匹配的记忆。":"这里还没有记忆。");
    return header("记忆","浏览事实、偏好和共同经历。需要补充、更正或遗忘时，在聊天中提出。")+
        '<div class="ds-page-body">'+tabs([["facts","事实与偏好"],["episodes","共同经历"],["buffer","待确认的理解"]],"memoryTab")+
        '<div class="ds-split"><section><label class="ds-search">'+icon("search")+'<input id="ds-memory-search" aria-label="查找记忆" placeholder="查找记忆" value="'+attr(state.query)+'"></label>'+
        '<div class="ds-list">'+rows.map((row,i)=>listButton("memory",i,row===selected,title(row),kind+" · "+friendly(row.recorded_at))).join("")+
        '</div>'+pagination("memory",result)+'</section><aside class="ds-detail">'+detail+'</aside></div>'+
        '<div class="ds-subtle-note">'+(state.memoryTab==="buffer"?"这些理解仍有不确定之处，会随着后续交流继续核对。":"角色的相处方式与成长，可在设置中的「角色与称呼」查看。")+'</div></div>';
}
function pluginPage() {
    const providers = state.data.plugins?.items || [];
    const catalog = state.data.tools?.items || [];
    const selected = providers[state.pluginIndex] || providers[0];
    const pluginIcon = id => ({schedule:"calendar-days",workspace:"folder-open",screen:"scan-line",agent:"sparkles",general:"clock"}[id] || "blocks");
    const top = header("工具与插件","查看聊天可用的工具、各插件提供的能力与页面。")+
        '<div class="ds-page-body">'+tabs([["catalog","能力目录"],["providers","插件"]],"pluginTab");
    if (state.pluginTab==="providers") {
        const detail = selected ? tag("内置")+'<h2>'+esc(selected.name)+'</h2><p>'+esc(selected.description)+'</p>'+
            pair("工具",(selected.tool_ids || []).length+" 项")+
            ((selected.event_sources || []).length?pair("事件源",selected.event_sources.map(event=>typeof event==="string"?event:event.name || event.id).join("、")):"")+
            (selected.page_title?pair("独立页面",selected.page_title):"")+
            buttonRow((selected.page_title?btn("打开插件页面","plugin-page",true,"arrow-up-right"):"")+btn("查看工具","plugin-tools")) : empty("尚无插件。");
        return top+'<div class="ds-split"><section class="ds-list">'+providers.map((row,i)=>listButton(
            "provider",i,row===selected,row.name,(row.tool_ids || []).length+" 项工具"+(row.page_title?" · 提供页面":""),"",pluginIcon(row.id))).join("")+
            '</section><aside class="ds-detail">'+detail+'</aside></div></div>';
    }
    const grouped = new Map();
    for (const tool of catalog) {
        const id = tool.plugin_id || "other";
        if (!grouped.has(id)) grouped.set(id,[]);
        grouped.get(id).push(tool);
    }
    const groups = [...grouped.entries()].map(([id,tools],index) => {
        const name = providers.find(row=>row.id===id)?.name || "其他能力";
        return '<details data-group="'+attr(id)+'" '+(state.openGroups.has(id)||!state.openGroups.size&&index===0?"open":"")+
            '><summary>'+esc(name)+' <span class="ds-muted">'+tools.length+' 项</span></summary>'+
            tools.map(tool=>'<button type="button" class="ds-tool-line" data-tool="'+attr(tool.id)+'"><span>'+esc(tool.display_name || tool.name)+'</span>'+icon("chevron-right")+'</button>').join("")+'</details>';
    }).join("");
    return top+'<label class="ds-filter">按插件筛选 <select id="ds-plugin-filter" aria-label="按插件筛选">'+option("all","全部插件",state.group==="all")+
        providers.map(row=>option(row.id,row.name,row.id===state.group)).join("")+'</select></label>'+
        '<div class="ds-tool-list">'+(groups || empty(state.loading==="tools"?"正在读取工具…":"没有符合条件的工具。"))+
        '</div>'+pagination("tools",state.data.tools || {})+'</div>';
}

const row = (title,desc,control) => '<div class="ds-setting-row"><div class="ds-setting-label">'+esc(title)+
    (desc?'<small>'+esc(desc)+'</small>':"")+'</div>'+control+'</div>';
const group = content => '<div class="ds-settings-group">'+content+'</div>';
const note = (title,text,marker="") => '<div class="ds-observation"><div class="ds-row ds-between"><h3>'+esc(title)+'</h3>'+
    (marker?tag(marker):"")+'</div><p>'+esc(text)+'</p></div>';
const toggle = (key,label,checked) => '<input type="checkbox" class="ds-switch" data-preference="'+key+'" aria-label="'+esc(label)+'" '+(checked?"checked":"")+'>';
const control = (key,value,label,type="text") => '<input class="ds-input" data-preference="'+key+'" type="'+type+'" value="'+attr(value)+'" aria-label="'+esc(label)+'">';
const select = (key,value,choices,label) => '<select data-preference="'+key+'" aria-label="'+esc(label)+'">'+choices.map(([id,title])=>option(id,title,id===value)).join("")+'</select>';
function imageRow(key,title,description) {
    const preview = key==="pet"?'<span class="ds-asset-pet">'+image("pet","cat")+'</span>':avatar(key,true);
    return '<div class="ds-setting-row ds-asset-row"><div class="ds-row ds-grow">'+preview+
        '<div class="ds-setting-label">'+esc(title)+'<small>'+esc(description)+'</small></div></div>'+
        '<div class="ds-asset-actions"><button type="button" class="ds-btn" data-image="'+key+'">选择图片</button>'+
        (state.data.appearance?.[key]?.asset_id?'<button type="button" class="ds-link" data-reset-image="'+key+'">恢复默认</button>':"")+'</div></div>';
}

function settingsContent() {
    const p = state.prefs;
    const model = state.data.settings || {};
    const policy = state.data.policy || {};
    const quiet = policy.quiet || {};
    const role = state.data.role || {};
    const card = role.character_card;
    const roleName = sender("assistant");
    switch (state.setting) {
    case "role":
        return '<h2>角色与称呼</h2>'+group(
            row("当前角色","每个角色拥有独立的聊天与数据",btn(roleName,"profiles",false,"chevron-down"))+
            row("角色的名字","聊天中显示的称呼",control("character_display_name",roleName,"角色的名字"))+
            row("如何称呼你","你的消息旁显示的名字",control("user_display_name",sender("user"),"如何称呼你")))+
            '<h3>聊天头像</h3>'+group(imageRow("role","角色头像","显示在角色消息和聊天标题旁")+imageRow("user","我的头像","显示在我发送的消息旁"))+
            group('<div class="ds-editor"><label for="ds-character">角色卡</label><textarea id="ds-character" '+(card?"":"disabled")+'>'+
                esc(state.draftCard ?? card?.text ?? "")+'</textarea><div class="ds-editor-foot">'+btn("重新读取","reset-card")+btn("保存角色卡","save-card",true)+'</div></div>')+
            '<h3>在交流中慢慢形成</h3>'+
            note("相处方式",(role.habits || []).map(item=>item.content).filter(Boolean).join("\n") || "还没有形成长期相处习惯。","自动学习")+
            note("当前情绪",role.current_emotion?.description_intensity || "目前没有需要保留的情绪状态。");
    case "model":
        return '<h2>模型连接</h2>'+group(
            row("模型运行位置","用于理解、回答和学习",tag(model.execution_location==="local"?"本地模型":"云端服务"))+
            row("服务商","兼容的模型服务",esc(model.provider==="offline"?"离线演示":"OpenAI 兼容服务"))+
            row("模型","当前连接使用的模型",esc(model.model || "尚未配置"))+
            row("API 密钥","保存在本机凭据存储",esc(model.credential_source==="system_vault"?"系统凭据库":model.credential_source==="environment"?"环境变量":"查看连接配置")))+
            '<details><summary>高级连接选项</summary>'+pair("协议",model.protocol || "")+pair("连接状态",model.llm_ready?"已就绪":"待配置")+'</details>'+
            buttonRow(btn("编辑连接与密钥","maintenance",true,"arrow-up-right"));
    case "appearance":
        return '<h2>桌宠与外观</h2>'+group(row("显示桌宠","在桌面显示当前桌宠形象",toggle("pet_visible","显示桌宠",p.pet_visible))+
            imageRow("pet","桌宠形象","用于桌面桌宠，与聊天头像独立")+
            row("桌宠大小","调整桌宠的显示比例",'<div class="ds-row"><input type="range" min="60" max="160" value="'+
                Math.round((p.pet_scale || 1)*100)+'" aria-label="桌宠大小" data-preference="pet_scale"><span id="ds-size-label">'+Math.round((p.pet_scale || 1)*100)+'%</span></div>')+
            row("桌宠对话小窗","单击桌宠即可开始聊天",btn("预览小窗","pet",false,"panel-right")))+
            group(row("界面主题","默认使用清透浅色",select("theme",p.theme,[["light","清透浅色"],["auto","跟随系统"],["dark","深色"]],"界面主题"))+
            row("内容间距","调整阅读的疏密程度",select("density",p.density,[["comfortable","舒适"],["compact","紧凑"]],"内容间距"))+
            row("桌宠置顶","保持在其他窗口上方",toggle("pet_on_top","桌宠置顶",p.pet_on_top)));
    case "notifications": {
        const asTime = n => String(Math.floor((n||0)/60)).padStart(2,"0")+":"+String((n||0)%60).padStart(2,"0");
        return '<h2>通知</h2>'+group(row("桌面通知","提醒到达时显示桌面通知",toggle("notifications_enabled","桌面通知",p.notifications_enabled))+
            row("安静时段","在指定时间减少打扰",toggle("quiet_enabled","安静时段",quiet.quiet_start!==quiet.quiet_end))+
            row("开始时间","每天",control("quiet_start",asTime(quiet.quiet_start),"安静时段开始","time"))+
            row("结束时间","次日或当天",control("quiet_end",asTime(quiet.quiet_end),"安静时段结束","time"))+
            row("定时提醒可突破免打扰","关闭时，到期提醒留在安排中",toggle("reminder_bypass","定时提醒可突破免打扰",quiet.reminder_bypass))+
            row("关闭主窗口后留在托盘","保持桌宠和后台可用",toggle("close_to_tray","关闭主窗口后留在托盘",p.close_to_tray)))+
            note("临时的需要，可以直接聊","“我想专心一会儿，晚点再聊。”\n一次性的要求会在交流中处理。");
    }
    case "data":
        return '<h2>数据与隐私</h2>'+group(
            row("当前角色的数据","聊天、记忆、人格和安排",btn("导出备份","backup",false,"download"))+
            row("恢复备份","从已有备份恢复角色数据",btn("选择备份","restore"))+
            row("聊天记录","按会话选择要清理的记录",btn("管理记录","clear-history")))+
            note("聊天记录与记忆分别管理","清空某个会话时，清除该会话的聊天记录和工作上下文。已经形成的长期记忆，可在记忆页查看，需要遗忘时在聊天中提出。")+
            '<details><summary>运行维护</summary>'+buttonRow(btn("配置、缓存与数据维护","maintenance")+btn("检查隐私清理","cleanup"))+'</details>';
    }
}

function settingsPage() {
    const nav = Object.entries(settings).map(([key,label])=>'<button type="button" class="'+(state.setting===key?"active":"")+
        '" data-setting="'+key+'">'+esc(label)+'</button>').join("");
    return header("设置")+'<div class="ds-settings-body"><nav class="ds-setting-nav" aria-label="设置分类">'+
        nav+'</nav><section class="ds-setting-content">'+settingsContent()+'</section></div>';
}

function render(options={}) {
    if(composing && state.page==="chat" && !options.reset) {queuedRender=options;return;}
    const thread = $("#ds-thread");
    const view = {top:thread?.scrollTop || 0, height:thread?.scrollHeight || 0, scroll:main.scrollTop,
        stick:!thread || thread.scrollHeight-thread.clientHeight-thread.scrollTop<96};
    const active = document.activeElement;
    const editable = active && main.contains(active) && active.matches("input, textarea");
    const focus = editable ? {id:active.id,name:active.name,key:active.dataset.preference,start:active.selectionStart,end:active.selectionEnd,
        scrollTop:active.scrollTop} : null;
    main.classList.toggle("ds-chat-page",state.page==="chat" && ["main","pet"].includes(state.mode));
    root.dataset.mode = state.mode;
    root.dataset.theme = state.prefs.theme==="dark" || state.prefs.theme==="auto" && matchMedia("(prefers-color-scheme: dark)").matches ? "dark":"light";
    root.dataset.density = state.prefs.density || "comfortable";
    root.querySelectorAll(".ds-nav [data-page]").forEach(button=>{
        button.classList.toggle("active",button.dataset.page===state.page);
        if (button.dataset.page===state.page) button.setAttribute("aria-current","page");
        else button.removeAttribute("aria-current");
    });
    main.innerHTML = ["maintenance","profiles"].includes(state.mode) ? servicePage() :
        ({chat:chatPage,plans:plansPage,memory:memoryPage,plugins:pluginPage,settings:settingsPage})[state.page]();
    if (!options.reset && !options.locate) main.scrollTop=view.scroll;
    if (focus && !options.reset) {
        const next = focus.id ? document.getElementById(focus.id) : [...main.querySelectorAll("input, textarea")].find(el=>focus.key?el.dataset.preference===focus.key:focus.name && el.name===focus.name);
        if (next && !next.disabled) {
            next.focus({preventScroll:true});
            if (focus.start!==null && focus.start!==undefined) next.setSelectionRange(focus.start,focus.end);
            next.scrollTop=focus.scrollTop;
        }
    }
    const updated = $("#ds-thread");
    if (updated) {
        const bottom = () => $(".ds-bottom").hidden=updated.scrollHeight-updated.clientHeight-updated.scrollTop<120;
        if (options.prepend) updated.scrollTop=view.top+updated.scrollHeight-view.height;
        else if (view.stick || options.reset) updated.scrollTop=updated.scrollHeight;
        else updated.scrollTop=view.top;
        updated.addEventListener("scroll",bottom);
        bottom();
        if (options.locate) [...updated.querySelectorAll("[data-task-card]")].find(row=>row.dataset.taskCard===options.locate)?.scrollIntoView({block:"nearest"});
    }
    resizeComposer($( '[data-form="message"] textarea' ));
}

function resizeComposer(input) {
    if(!input) return;
    const compact=state.mode==="pet";
    const maximum=compact?150:240;
    input.style.height="auto";
    input.style.height=Math.min(input.scrollHeight,maximum)+"px";
    input.style.overflowY=input.scrollHeight>maximum?"auto":"hidden";
}

function readPage() {
    switch(state.page) {
    case "plans": readList("plans"); break;
    case "memory": readList("memory"); break;
    case "plugins":
        send("load",{key:"plugins"});
        if(state.pluginTab==="catalog") readList("tools");
        break;
    case "settings":
        send("load",{key:"settings"});
        if(state.setting==="role") send("load",{key:"role"});
        if(state.setting==="notifications") send("load",{key:"policy"});
        break;
    }
}

let serial = 0;
const revisions = {};
function readList(key) {
    const request = {key, offset:state.offsets[key], request:++serial};
    revisions[key] = request.request;
    if (key==="plans") request.tab=state.planTab;
    if (key==="memory") { request.tab=state.memoryTab; request.query=state.query; }
    if (key==="tools") request.group=state.group;
    state.loading=key;
    send("load",request);
}

function navigate(page,setting) {
    if (state.mode==="pet" || !(["chat","plans","memory","plugins","settings"].includes(page))) return;
    state.page=page;
    if (settings[setting]) state.setting=setting;
    state.menu=false;
    if (page!=="chat") state.execution=false;
    closeModal();
    send("route",{page,setting:state.setting});
    render({reset:true});
    readPage();
}

function planSelected() {
    const rows=state.data.plans?.items || [];
    return rows[state.planIndex] || rows[0];
}
function memorySelected() {
    const rows=state.data.memory?.items || [];
    return rows[state.memoryIndex] || rows[0];
}
function providerSelected() {
    const rows=state.data.plugins?.items || [];
    return rows[state.pluginIndex] || rows[0];
}
function pluginModal(data) {
    modal(data.title || providerSelected()?.page_title || "插件页面",'<div class="ds-section-label">最近到期记录</div>'+
        (data.switches || []).map(control=>'<label class="ds-setting-row ds-plugin-filter"><span>'+esc(control.label)+
            '</span><input type="checkbox" class="ds-switch" data-option="'+attr(control.key)+'" data-plugin="'+attr(data.plugin_id)+
            '" data-revision="'+attr(control.revision)+'" '+(control.value?"checked":"")+'></label>').join("")+
        '<div class="ds-list">'+((data.rows || []).map(item=>row(item.title,item.text || "",'<span class="ds-muted">'+esc(friendly(item.occurred_at))+'</span>')).join("") ||
        empty("还没有符合条件的记录。"))+'</div>');
}

function dispatchAction(action) {
    switch(action) {
    case "close-modal": closeModal(); return;
    case "session-menu":
        state.menu=!state.menu;
        $(".ds-menu").hidden=!state.menu;
        $(".ds-session-button").setAttribute("aria-expanded",String(state.menu));
        return;
    case "new-session": modal("新建专项会话",'<p class="ds-muted">为一件事留一个专门的聊天空间。</p>'+
        form("new-session",field("title","会话名称","","text",'required maxlength="120"'),"开始聊天")); return;
    case "manage-session": modal("管理专项会话",form("rename",field("title","会话名称",state.chat.title,"text",'required maxlength="120"'),"保存名称")+
        buttonRow(btn(state.chat.archived?"恢复会话":"归档会话","archive-session")+btn("删除会话","delete-session"))); return;
    case "delete-session": modal("删除专项会话",'<p>删除这段会话及其中的聊天记录？</p>'+buttonRow(btn("确认删除","confirm-delete",true))); return;
    case "archive-session": send("archive-session"); closeModal(); return;
    case "confirm-delete": send("delete-session"); closeModal(); return;
    case "more-sessions": case "archived-sessions": case "older": case "expand-chat": case "pet":
        send(action); return;
    case "bottom":
        if (state.chat.history) send("latest");
        else $("#ds-thread").scrollTop=$("#ds-thread").scrollHeight;
        return;
    case "execution":
        state.execution=!state.execution;
        if (state.execution) send("records");
        else { state.task=""; send("close-records"); }
        render(); return;
    case "plan-chat": {
        const row=planSelected();
        if(row) send("plan-chat",{id:row.id, tab:state.planTab});
        return;
    }
    case "memory-source": {
        const item=memorySelected();
        if(item) modal("交流来源",'<div class="ds-muted">'+esc(friendly(item.source?.created_at || item.recorded_at))+'</div>'+
            '<div style="margin-top:22px">'+message("user",item.draft?.quote || item.source?.content || "没有保留原文摘录。")+'</div>'+
            (item.source?.session_id?buttonRow(btn("前往会话","memory-chat",true)):""));
        return;
    }
    case "memory-chat": {
        const item=memorySelected();
        if(item) send("memory-chat",{id:item.id});
        return;
    }
    case "plugin-page": {const plugin=providerSelected(); if(plugin) send("plugin-page",{id:plugin.id}); return;}
    case "plugin-tools": {
        const plugin=providerSelected();
        if (plugin) {
            state.group=plugin.id; state.pluginTab="catalog"; state.offsets.tools=0; state.openGroups.clear(); render(); readList("tools");
        }
        return;
    }
    case "save-card": {
        const text=$("#ds-character")?.value || "";
        if(text.trim()) send("save-card",{text});
        return;
    }
    case "reset-card":
        state.draftCard=null; send("role-reverted"); send("load",{key:"role"}); render(); return;
    case "profiles": case "backup": case "restore": case "maintenance": case "cleanup":
        send(action); return;
    case "asset-save": case "asset-discard":
        overlay.querySelector(".ds-preview-image")?.remove();
        send(action); closeModal(); return;
    case "clear-history":
        modal("管理聊天记录",'<p class="ds-muted">清理记录后保留会话入口，长期记忆可在记忆页查看。</p>'+
            form("clear-history",'<label class="ds-field">选择会话<select name="id">'+
                (state.chat.sessions || []).map(item=>option(item.id,item.title,item.id===state.chat.id)).join("")+
                '</select></label><label><input required type="checkbox"> 确认清空所选会话的聊天记录</label>',"清空聊天记录"));
        return;
    default: {
        const match=/^(prev|next)-(plans|memory|tools)$/.exec(action);
        if (match) {
            const key=match[2];
            state.offsets[key]=Math.max(0,state.offsets[key]+(match[1]==="next"?50:-50));
            readList(key);
        } else serviceAction(action);
    }
    }
}

root.addEventListener("click",event=>{
    if (event.target===overlay) { closeModal(); return; }
    const el=event.target.closest("button");
    if (!el) {
        if (state.menu && !event.target.closest(".ds-session-wrap")) {
            state.menu=false;
            if ($(".ds-menu")) $(".ds-menu").hidden=true;
        }
        return;
    }
    const d=el.dataset;
    if(d.page) return navigate(d.page);
    if(d.setting) return navigate("settings",d.setting);
    if(d.tab) {
        state[d.tab]=d.value;
        const key={planTab:"plans",memoryTab:"memory",pluginTab:"tools"}[d.tab];
        state.offsets[key]=0; state.planIndex=state.memoryIndex=0;
        state.data[key]={};
        render({reset:true}); readPage(); return;
    }
    if(d.session) {state.menu=false; send("session",{id:d.session}); return;}
    if(d.plan!==undefined) {state.planIndex=Number(d.plan); render(); return;}
    if(d.memory!==undefined) {state.memoryIndex=Number(d.memory); render(); return;}
    if(d.provider!==undefined) {state.pluginIndex=Number(d.provider); render(); return;}
    if(d.tool) return send("tool",{id:d.tool});
    if(d.event) return send("event",{id:d.event});
    if(d.record) {state.execution=true; send("records",{id:d.record}); return;}
    if(d.retry) return send("retry",{id:d.retry});
    if(d.message) return send("message-part",{id:d.message,offset:Number(d.offset)});
    if(d.image) return send("asset-pick",{key:d.image});
    if(d.resetImage) return send("asset-reset",{key:d.resetImage});
    if(d.taskAction) {
        if(["review_uncertain","task_answer","task_append_requirement"].includes(d.taskAction)) {
            modal(actions[d.taskAction],form("task-input",'<input name="id" type="hidden" value="'+attr(d.task)+'">'+
                '<input name="action" type="hidden" value="'+attr(d.taskAction)+'">'+
                '<input name="revision" type="hidden" value="'+attr(state.chat.reviews?.[d.task]?.review?.task_revision)+'">'+
                (d.taskAction==="review_uncertain"?'<label class="ds-field">判断<select name="verdict"><option value="unknown">尚无法判断</option><option value="occurred">已发生</option><option value="not_occurred">未发生</option></select></label>':"")+
                field("text",d.taskAction==="review_uncertain"?"核实依据":"补充内容","","text","required"),"提交"));
        } else {
            const revision=state.chat.reviews?.[d.task]?.review?.task_revision;
            send("task-action",{id:d.task, action:d.taskAction, revision});
        }
        return;
    }
    if(d.action) dispatchAction(d.action);
});

root.addEventListener("submit",event=>{
    const form=event.target.closest("form[data-form]");
    if(!form) return;
    event.preventDefault();
    const data=Object.fromEntries(new FormData(form));
    const name=form.dataset.form;
    if(name==="message") {
        if(data.text?.trim()) send("submit",{text:data.text,id:state.chat.id});
    } else {
        if(name==="task-input") data.revision=Number(data.revision);
        send(name,data);
        if (["new-session","rename","clear-history","task-input"].includes(name)) closeModal();
        if(name==="save-connection") form.querySelector("[name=secret]").value="";
    }
});
root.addEventListener("compositionstart",event=>{
    if(event.target.matches('[data-form="message"] textarea')) composing=true;
});
root.addEventListener("compositionend",()=>{
    composing=false;
    if(queuedRender){const pending=queuedRender;queuedRender=null;requestAnimationFrame(()=>render(pending));}
});
root.addEventListener("keydown",event=>{
    if(event.key==="Escape") {
        if(!overlay.hidden) closeModal();
        else if(state.menu) {state.menu=false; $(".ds-menu").hidden=true;}
        else if(state.mode==="pet") send("hide");
    }
    if(event.target.matches('[data-form="message"] textarea')&&event.key==="Enter"&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229) {
        event.preventDefault();
        if(state.chat.canSend&&!event.repeat) event.target.form.requestSubmit();
    }
});
root.addEventListener("input",event=>{
    const el=event.target;
    if(el.matches('[data-form="message"] textarea')) {
        resizeComposer(el);
        state.chat.draft=el.value;
        send("draft",{id:state.chat.id,text:el.value});
    } else if(el.id==="ds-memory-search") {
        state.query=el.value.slice(0,200); state.offsets.memory=0; state.memoryIndex=0;
        clearTimeout(searchTimer); searchTimer=setTimeout(()=>readList("memory"),220);
    } else if(el.id==="ds-character") {
        state.draftCard=el.value; send("role-dirty",{value:true});
    } else if(el.dataset.preference==="pet_scale") {
        $("#ds-size-label").textContent=el.value+"%";
    }
});
root.addEventListener("change",event=>{
    const el=event.target;
    if(el.dataset.grant) send("grant",{id:el.dataset.task, tool:el.dataset.grant, enabled:el.checked,
        revision:state.chat.reviews?.[el.dataset.task]?.review?.task_revision});
    if(el.dataset.preference) send("preference",{key:el.dataset.preference,value:el.type==="checkbox"?el.checked:el.value});
    if(el.dataset.option) send("plugin-option",{id:el.dataset.plugin,key:el.dataset.option,value:el.checked,revision:Number(el.dataset.revision)});
    if(el.id==="ds-plugin-filter") {
        state.group=el.value; state.offsets.tools=0; state.openGroups.clear(); readList("tools");
    }
});
root.addEventListener("toggle",event=>{
    if(event.target.dataset.group) {
        if(event.target.open) state.openGroups.add(event.target.dataset.group);
        else state.openGroups.delete(event.target.dataset.group);
    }
},true);

function receive(kind,data) {
    switch(kind) {
    case "init":
        state.mode=data.mode;
        state.lifecycle=data.lifecycle || "Ready";
        if (data.mode==="maintenance" || data.mode==="profiles") {state.page="settings"; state.setting=data.mode==="maintenance"?"model":"data";}
        state.prefs=data.prefs || {};
        render({reset:true}); readPage(); return;
    case "route": navigate(data.page,data.setting); return;
    case "state": state.lifecycle=data.state; state.status=data.text; render(); return;
    case "clear":
        state.chat={};state.images={};state.data={};state.query="";state.draftCard=null;
        closeModal();render({reset:true});return;
    case "chat": {
        const changed=state.chat.id!==data.id;
        state.chat=data;
        if(changed) {state.execution=false;state.task="";}
        if(data.locate) state.menu=false;
        render({reset:changed,prepend:data.prepend,locate:data.locate});
        return;
    }
    case "draft":
        if(state.chat.id===data.id) {
            state.chat.draft=data.text;
            const input=$('[data-form="message"] textarea');
            if(input && !composing && input.value!==data.text) {input.value=data.text;resizeComposer(input);}
        }
        return;
    case "data":
        if(data.request && revisions[data.key]!==data.request) return;
        state.data[data.key]=data.value;
        if(state.loading===data.key) state.loading="";
        if(data.key==="role" && data.saved) state.draftCard=null;
        if(state.page==="settings" || state.page==="plans"&&data.key==="plans" ||
            state.page==="memory"&&data.key==="memory" || state.page==="plugins"&&["plugins","tools"].includes(data.key)) render();
        return;
    case "prefs": state.prefs=data; render(); return;
    case "image": state.images[data.key]=data.url; render(); return;
    case "asset-preview":
        modal("预览图片",'<img class="ds-preview-image" src="'+attr(safeImage(data.url))+'" alt="图片预览">'+
            buttonRow(btn("取消预览","asset-discard")+btn("保存应用","asset-save",true)));
        return;
    case "record": state.task=data.id; render(); return;
    case "schedule-detail":
        modal(data.title || "安排",pair("时间",friendly(data.start_at || data.due_at))+
            (data.end_at?pair("结束时间",friendly(data.end_at)):"")+
            (data.reminder_at?pair("提醒时间",friendly(data.reminder_at)):""));
        return;
    case "plugin":
        if(data._error) toast("插件页面读取失败。"); else pluginModal(data);
        return;
    case "tool":
        modal(data.display_name || data.name || "工具详情",'<p>'+esc(data.summary || data.description || "")+'</p>'+
            pair("执行权限",({0:"按需使用",1:"需要授权",2:"需要额外确认"})[data.permission_level] || "由工具配置决定")+
            (data.parameter_fields?.length?'<details><summary>输入参数</summary>'+data.parameter_fields.map(item=>pair(item.label || item.name,item.required?"必填":"可选")).join("")+'</details>':""));
        return;
    case "service":
        serviceState=data; render();
        if(data.initialAction==="restore" && !restorePrompted) {
            restorePrompted=true;
            serviceAction("restore-profile");
        }
        return;
    case "toast": toast(String(data)); return;
    }
}

root.querySelectorAll("i[data-lucide]").forEach(el=>{el.outerHTML=icon(el.dataset.lucide);});
if (typeof qt !== "undefined") {
    new QWebChannel(qt.webChannelTransport,channel=>{
        port=channel.objects.studio;
        port.message.connect(raw=>{const message=JSON.parse(raw); receive(message.kind,message.data);});
        port.attach();
    });
}
let serviceState={};
let serviceProfile="";
let restorePrompted=false;
let maintenanceTab="model";
function servicePage() {
    const snapshot=serviceState.snapshot || {};
    const badge=serviceState.message ? '<p class="ds-section-note">'+esc(serviceState.message)+'</p>' : "";
    const disabled=serviceState.busy ? " disabled" : "";
    const nav=state.mode==="maintenance" ? [["model","模型连接"],["cache","模型缓存"],["backups","备份与检查"]] : [["data","角色档案"]];
    const sidebar=nav.map(([id,name])=>'<button type="button" class="'+((state.mode==="profiles"||id===maintenanceTab)?"active":"")+
        '" data-action="maintenance-'+id+'">'+esc(name)+'</button>').join("");
    let body="";
    if(state.mode==="profiles") {
        const items=snapshot.profiles || [];
        const selected=items.find(item=>item.id===serviceProfile) || items.find(item=>item.id===snapshot.selected_id) || items[0];
        body='<h2>角色档案</h2>'+badge+'<div class="ds-list">'+items.map(item=>
            '<button type="button" class="ds-list-item '+(item===selected?"active":"")+'" data-profile="'+attr(item.id)+'"'+disabled+'>'+
            avatar()+'<span class="ds-grow"><h3>'+esc(item.name)+'</h3></span>'+(item.id===snapshot.selected_id?tag("当前选择"):"")+'</button>').join("")+'</div>'+
            buttonRow((selected?btn("打开角色","open-profile",true)+btn("重命名","rename-profile")+btn("导出备份","backup-profile")+btn("删除档案","delete-profile"):"")+
            btn("新建角色","create-profile",false,"plus")+btn("选择备份","restore-profile",false,"download"))+
            buttonRow(btn("模型连接与维护","service-maintenance")+btn("退出","service-close"));
    } else if(maintenanceTab==="model") {
        const v=snapshot.values || {};
        const stored=(key,fallback="")=>v[key] ?? fallback;
        const dropdown=(name,choices,def)=>'<select name="'+name+'">'+choices.map(([id,label])=>option(id,label,String(stored(name,def))===id)).join("")+'</select>';
        const edit=(key,def="",type="text")=>'<input name="'+key+'" class="ds-input" type="'+type+'" value="'+attr(stored(key,def))+'" maxlength="4096">';
        const connection='<form data-form="save-connection"><fieldset class="ds-service-fields"'+disabled+'>'+
            group(row("模型运行位置","用于理解、回答和学习",dropdown("execution_location",[["cloud","云端服务"],["local","本地模型"]],"cloud"))+
            row("服务商","兼容 Responses 接口的服务",dropdown("provider",[["openai","兼容服务"],["offline","离线演示"]],"openai"))+
            row("模型","当前连接使用的模型",edit("model"))+
            row("API 密钥","保存在本机凭据存储",'<input type="password" class="ds-input" name="secret" maxlength="8192" autocomplete="off" placeholder="留空保留现有密钥">'))+
            '<details><summary>高级连接选项</summary>'+
            row("服务地址","",edit("base_url","https://api.openai.com/v1"))+
            row("连接协议","",dropdown("protocol",[["responses","Responses"]],"responses"))+
            row("凭据来源","",dropdown("credential_mode",[["system_vault","系统凭据库"],["environment","环境变量"],["none","无需凭据"]],"system_vault"))+
            row("凭据操作","填写密钥时自动选择替换",'<select name="credential_action"><option value="keep">保留</option><option value="replace">替换</option><option value="remove">移除</option></select>')+
            row("密钥环境变量名","",edit("api_key_env","OPENAI_API_KEY"))+
            row("超时秒数","",edit("timeout_seconds",30,"number"))+
            row("表达格式","",dropdown("expression_output_format",[["prompt","自然语言正文"],["json_object","原生 JSON"]],"prompt"))+
            row("允许新聊天输入由云服务处理","",dropdown("request_use_default",[["true","允许"],["false","关闭"]],"true"))+
            row("工作区目录","",edit("workspace_dir"))+row("数据目录","",edit("data_dir"))+
            row("记忆写入","",dropdown("memory_mode",[["automatic","自动学习"],["paused","暂停"]],"automatic"))+
            row("语义检索","",dropdown("embedding_provider",[["disabled","关键词检索"],["fastembed","本地语义检索"]],"disabled"))+
            row("模型缓存目录","",edit("model_cache_dir"))+'</details>'+
            (snapshot.config_exists && snapshot.config_valid===false?'<label><input type="checkbox" name="confirm_repair" required> 确认重建损坏配置</label>':"")+
            '<div class="ds-detail-footer">'+btn("重新读取","service-reload")+btn("测试连接","ask-connection-test")+
            '<button class="ds-btn ds-primary" type="submit"'+(!serviceState.snapshot?" disabled":"")+'>保存连接</button></div></fieldset></form>';
        body='<h2>模型连接</h2>'+badge+(snapshot.issues || []).map(issue=>'<p class="ds-section-note">'+esc(issue.message)+'</p>').join("")+connection+
            buttonRow(btn(snapshot.llm_ready?"打开工作室":"打开工作室（聊天待配置）","service-start",true)+btn("角色档案","service-profiles"));
    } else if(maintenanceTab==="cache") {
        body='<h2>模型缓存</h2>'+badge+note("本机语义检索","管理当前配置使用的语义检索模型缓存。")+
            buttonRow(btn("读取缓存状态","cache-status")+btn("校验缓存","cache-verify")+btn("准备模型","ask-cache-prepare")+btn("清理缓存","ask-cache-clear"));
    } else {
        body='<h2>备份与检查</h2>'+badge+group(row("角色档案","导出和恢复档案","<button type='button' class='ds-btn' data-action='service-profiles'>打开角色档案</button>")+
            row("检查归档","核对备份完整性",btn("选择备份","backup-inspect")));
    }
    if(Object.keys(serviceState.receipt || {}).length) body+='<details><summary>操作回执</summary>'+structured(serviceState.receipt)+'</details>';
    return header("设置")+'<div class="ds-settings-body"><nav class="ds-setting-nav">'+sidebar+
        '</nav><section class="ds-setting-content">'+body+'</section></div>';
}
function serviceAction(action) {
    if(serviceState.busy) return;
    if(state.mode==="maintenance" && action.startsWith("maintenance-")) {
        maintenanceTab=action.slice(12); render(); return;
    }
    const items=serviceState.snapshot?.profiles || [];
    const profile=items.find(row=>row.id===serviceProfile)||items.find(row=>row.id===serviceState.snapshot?.selected_id)||items[0];
    if(action==="open-profile"||action==="backup-profile") {
        if(profile) send(action==="open-profile"?"profile-select":"profile-backup",{id:profile.id});
        return;
    }
    const forms={"create-profile":["新建角色","profile-create"],"rename-profile":["重命名角色","profile-rename"],"restore-profile":["导入角色备份","profile-restore"]};
    if(forms[action]) {
        const [title,name]=forms[action];
        modal(title,form(name,field("name","角色名称",action==="rename-profile"?profile?.name:"","text",'required maxlength="120"')+
            '<input type="hidden" name="id" value="'+attr(profile?.id)+'">',action==="restore-profile"?"选择备份":"保存"));
        return;
    }
    if(action==="delete-profile" && profile) {
        modal("删除角色",form("profile-delete",'<input type="hidden" name="id" value="'+attr(profile.id)+'">'+
            '<label><input type="checkbox" name="confirmed" required> 删除该角色档案及其中的数据</label>',"删除"));
        return;
    }
    const confirms={
        "ask-connection-test":["测试连接","test-connection","使用已保存的连接发送一条固定测试消息，可能产生少量费用。"],
        "ask-cache-prepare":["准备模型","cache-prepare","允许本次从配置的源下载语义检索模型。"],
        "ask-cache-clear":["清理缓存","cache-clear","确认清理已读取的本机模型缓存。"]
    };
    if(confirms[action]) {
        const [title,name,text]=confirms[action];
        modal(title,form(name,'<label><input type="checkbox" name="confirmed" required> '+esc(text)+'</label>',"确认"));
    } else send(action);
}
root.addEventListener("click",event=>{
    const profile=event.target.closest("[data-profile]");
    if(profile){serviceProfile=profile.dataset.profile;render();}
});
root.addEventListener("input",event=>{
    if(event.target.matches('[data-form="save-connection"] [name="secret"]') && event.target.value)
        event.target.form.querySelector("[name=credential_action]").value="replace";
});
