"use strict";
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const statuses={Created:"排队中",Planning:"正在思考",Running:"正在处理",Waiting:"等待确认",NeedsReview:"需要复核",AwaitingInput:"等待补充",Paused:"已暂停",Completed:"已完成",Failed:"未完成",Cancelled:"已取消",Uncertain:"结果待核实"};
const active = status=>["Created","Planning","Running","Waiting","AwaitingInput","Paused"].includes(status);
const actions={confirm:"确认并继续",cancel:"取消请求",task_pause:"暂停",task_continue:"继续",task_answer:"补充信息",task_append_requirement:"追加要求"};
const S={session:null,id:"",directory:[],cursor:null,items:[],epoch:null,privacy:null,timelineRevision:0,older:null,loaded:false,reviews:{},grants:{},busy:false,polling:false,pending:null,target:null,targetChosen:false,generation:0,signature:"",lastSession:0,lastDirectory:0};
let toastTimer, composing=false;
const dog='<path d="M7 6C3 6 2 12 4 15l3-1m10-8c4 0 5 6 3 9l-3-1M7 6q5-4 10 0v10q0 5-5 5t-5-5Zm2 6h.1m5.9 0h.1M10 16l2 1 2-1"/>';
const cat='<path d="M4 14V5l5 3h6l5-3v9c0 5-4 7-8 7s-8-2-8-7Zm4 0h.1m7.9 0h.1M10 17l2 1 2-1"/>';
const avatar = user=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(user?dog:cat)+'</svg>';
function notice(text){$("#notice").textContent=text;$("#notice").hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("#notice").hidden=true,4500);}
function storage(key,value){try{if(value===undefined)return sessionStorage.getItem(key);if(value===null)sessionStorage.removeItem(key);else sessionStorage.setItem(key,value);}catch{}return null;}
function pendingKey(){return "deskpal-pending:"+S.session.instance_id;}
function draftKey(){return "deskpal-draft:"+S.session.instance_id+":"+S.id;}
function id(){if(globalThis.crypto?.randomUUID)return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes,v=>v.toString(16).padStart(2,"0")).join("");}
async function request(path, body){
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),20000);
    try{
        const response=await fetch(path,{method:body?"POST":"GET",credentials:"same-origin",cache:"no-store",signal:controller.signal,
            headers:body?{"Content-Type":"application/json","X-Deskpal-CSRF":S.session?.csrf || ""}:{},body:body?JSON.stringify(body):undefined});
        const data=await response.json();
        if(!response.ok || data.ok===false){const error=new Error(data.error?.message || data.error || "操作未完成。");error.code=data.error?.code || data.code;error.status=response.status;throw error;}
        return data;
    }finally{clearTimeout(timer);}
}
async function rpc(operation, parameters={}){
    const instance=S.session.instance_id,generation=S.generation;
    let data;
    try{data=await request("/api/command",{instance_id:instance,operation,parameters});}
    catch(error){if(generation!==S.generation){error.code="Stale";}throw error;}
    if(S.session?.instance_id!==instance){const error=new Error("后台已变化，请重新连接。");error.code="Changed";throw error;}
    return data.data;
}
function errorMessage(error){if(error.code==="Stale")return;if(error.code==="Unauthorized"||error.status===401){login();return;}$("#connection").textContent=error.code?error.message:"连接已中断，正在等待重连。";if(!S.session){$("#login").hidden=false;notice("暂时无法连接电脑上的 Deskpal，请检查应用和网络。");}}
function clearDetails(){ $("#detail").close();$("#detail-body").replaceChildren();$("#directory").hidden=true;$("#directory").replaceChildren();S.directorySignature=""; }
function login(){S.generation++;S.session=null;S.items=[];S.reviews={};S.loaded=false;clearDetails();$("#messages").replaceChildren();$("#input").value="";$("#app").hidden=true;$("#login").hidden=false;}
async function connect(){
    const generation=S.generation;
    try{
        const session=await request("/api/session");
        if(generation!==S.generation)return false;
        const previous=S.session?.instance_id || storage("deskpal-instance");
        if(previous && previous!==session.instance_id){
            S.generation++;S.items=[];S.reviews={};S.grants={};S.id="";S.pending=null;S.signature="";
            clearDetails();$("#messages").replaceChildren();S.loaded=false;
            $("#input").value="";
            notice("后台已重新启动或切换角色，旧请求不会自动重发。请查看原会话确认结果。");
        }
        S.session=session;S.lastSession=Date.now();storage("deskpal-instance",session.instance_id);
        if(!S.pending){try{S.pending=JSON.parse(storage(pendingKey()) || "null");}catch{storage(pendingKey(),null);}}
        $("#login").hidden=true;$("#app").hidden=false;
        await directory();
        if(!S.id)await selectConversation(S.defaultId || S.directory[0]?.id);
        else await refresh();
        showPending();
        return true;
    }catch(error){if(generation!==S.generation)return false;if(error.status===401)login();else errorMessage(error);return false;}
}
async function directory(more=false){
    const generation=S.generation;
    const data=await rpc("conversation_list",{limit:50,...(more&&S.cursor?{cursor:S.cursor}:{})});
    if(generation!==S.generation)return;
    S.directory=more?[...S.directory,...data.items]:data.items;S.cursor=data.next_cursor;S.defaultId=data.default_id;S.lastDirectory=Date.now();
    const html=S.directory.map(c=>'<button data-session="'+esc(c.id)+'" class="'+(c.id===S.id?"selected":"")+'">'+esc(c.title)+(c.id===S.defaultId?' · 固定':"")+'</button>').join("")+
        (S.cursor?'<button data-more="1" class="more">更多会话</button>':"")+'<button data-new="1">＋ 新建专项会话</button>';
    if(html!==S.directorySignature){$("#directory").innerHTML=html;S.directorySignature=html;}
}
async function selectConversation(identity){
    if(!identity)return;
    if(S.id && S.session)storage(draftKey(),$("#input").value);
    S.id=identity;S.items=[];S.reviews={};S.grants={};S.epoch=null;S.privacy=null;S.older=null;S.loaded=false;S.target=null;S.targetChosen=false;S.signature="";S.generation++;
    $("#input").value=storage(draftKey()) || "";$("#input").disabled=true;$("#send").disabled=true;$("#directory").hidden=true;$("#messages").replaceChildren();
    await refresh(true);
}
function absorb(data, prepend=false){
    const reset=S.epoch!==null&&(S.epoch!==data.epoch || S.privacy!==data.privacy_epoch);
    const gap=S.loaded&&data.reset_required&&data.has_more&&data.items[0]?.sequence>(S.items.at(-1)?.sequence || 0)+1;
    if(reset){S.items=[];S.reviews={};S.grants={};S.older=null;S.loaded=false;S.target=null;S.signature="";$("#detail").close();$("#detail-body").replaceChildren();}
    S.epoch=data.epoch;S.privacy=data.privacy_epoch;S.conversation=data.conversation;
    // 旧页可能读到了更新的总版本，但没有包含最新页正文；下一次强制补齐。
    S.timelineRevision=prepend?0:data.timeline_revision;
    const merged=new Map(S.items.map(item=>[item.id,item]));
    for(const item of data.items)merged.set(item.id,item);
    S.items=Array.from(merged.values()).sort((a,b)=>a.sequence-b.sequence);
    if(prepend || !S.loaded || gap)S.older=data.has_more?data.next_cursor:null;
    S.loaded=true;
    $("#directory-button").textContent=(data.conversation?.title || "日常聊天")+"⌄";
}
async function refresh(initial=false){
    if(!S.id||!S.session)return;
    const identity=S.id,generation=S.generation;
    const after=S.loaded?{after:S.items.at(-1)?.sequence || 0,known_revision:S.timelineRevision}:{};
    const data=await rpc("conversation_timeline",{id:identity,limit:50,...after});
    if(generation!==S.generation)return;
    absorb(data);
    const taskItems=S.items.filter(item=>item.task_id && item.kind==="task_card").slice(-12);
    const taskIds=taskItems.map(item=>item.task_id);
    const known=Object.fromEntries(taskIds.filter(key=>S.reviews[key]).map(key=>[key,S.reviews[key].task_revision]));
    const updates=taskIds.length?await rpc("task_updates",{task_ids:taskIds,known_revisions:known}):{items:[]};
    if(generation!==S.generation)return;
    const changed=new Set(updates.items.map(item=>item.task_id));
    for(const item of taskItems){
        if(!S.reviews[item.task_id] || changed.has(item.task_id)){
            const review=await rpc("task_review",{task_id:item.task_id});
            if(generation!==S.generation)return;
            const previous=S.reviews[item.task_id];
            if(previous&&(previous.plan_hash!==review.plan_hash||previous.task_revision!==review.task_revision))delete S.grants[item.task_id];
            S.reviews[item.task_id]=review;
        }
    }
    render(initial);
    $("#connection").textContent="已连接 · 与电脑同步";
}
function fields(value,depth=0){
    if(value===null||value===undefined)return "";
    if(typeof value!=="object"){
        if(typeof value==="boolean")return value?"是":"否";
        if(typeof value==="string"&&/^\d{4}-\d{2}-\d{2}T/.test(value)&&!Number.isNaN(Date.parse(value)))return esc(new Date(value).toLocaleString("zh-CN",{year:"numeric",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"}));
        return esc(String(value));
    }
    if(depth>3)return "更多信息请在电脑端查看";
    if(Array.isArray(value))return value.length?value.slice(0,30).map(item=>'<div>'+fields(item,depth+1)+'</div>').join(""):"暂无条目";
    const names={content:"内容",text:"内容",label:"名称",title:"标题",seconds:"时长（秒）",due_at:"提醒时间",start_at:"开始时间",end_at:"结束时间",note:"备注",limit:"数量上限",items:"条目",success:"是否成功",message:"说明",error:"原因",time:"时间",current_time:"当前时间",timezone:"时区",status:"状态"};
    return '<div class="fields">'+Object.entries(value).filter(([key])=>!["call_id","task_id","source_id"].includes(key)).slice(0,25).map(([key,v])=>'<div><span>'+esc(names[key]||key)+'：</span>'+fields(v,depth+1)+'</div>').join("")+'</div>';
}
function taskCard(item){
    const r=S.reviews[item.task_id];
    if(!r)return item.visible?'<div class="task"><span class="task-status">'+esc(statuses[item.task?.status]||"任务记录")+'</span><button data-detail="'+esc(item.task_id)+'">查看任务</button></div>':"";
    if(!item.visible && !active(r.status) && !["Failed","Uncertain","NeedsReview"].includes(r.status))return "";
    const buttons=(r.available_actions || []).filter(action=>actions[action]);
    const confirming=buttons.includes("confirm"), high=r.missing_permissions?.high_risk_tools || [];
    const grants=S.grants[item.task_id] || [];
    return '<article class="task"><h3>'+esc(r.title || "任务")+'</h3><span class="task-status">'+esc(statuses[r.status]||r.status)+'</span>'+
        (r.blocked_reason?'<p>'+esc(r.blocked_reason)+'</p>':"")+
        (confirming?'<ol>'+(r.steps||[]).map(step=>'<li><strong>'+esc(step.display_name || step.tool_name)+'</strong>'+fields(step.arguments)+'</li>').join("")+'</ol>'+high.map(tool=>'<label><input type="checkbox" data-grant="'+esc(tool)+'" data-task="'+esc(item.task_id)+'" '+(grants.includes(tool)?"checked":"")+'>允许 '+esc((r.steps||[]).find(step=>step.tool_name===tool)?.display_name||tool)+'</label>').join(""):"")+
        '<div class="buttons">'+buttons.map(action=>'<button data-task="'+esc(item.task_id)+'" data-action="'+action+'" '+(S.busy || S.pending || confirming&&action==="confirm"&&high.some(tool=>!grants.includes(tool))?"disabled":"")+'>'+actions[action]+'</button>').join("")+
        '<button data-detail="'+esc(item.task_id)+'">执行记录</button></div></article>';
}
function render(bottom=false){
    const thread=$("#thread"),stick=thread.scrollHeight-thread.clientHeight-thread.scrollTop<100,top=thread.scrollTop;
    const html=S.items.filter(item=>!item.redacted_at).map(item=>{
        if(item.kind==="task_card")return taskCard(item);
        if(!["user_message","assistant_message"].includes(item.kind)||!item.body)return "";
        const user=item.kind==="user_message", name=S.session.names[user?"user":"assistant"];
        return '<article class="message '+(user?"user":"assistant")+'"><div class="author">'+(user?esc(name)+avatar(true):avatar(false)+esc(name))+'</div><div class="body">'+esc(item.body)+'</div>'+
            (item.body_truncated?'<button data-full="'+esc(item.id)+'">继续阅读</button>':"")+'</article>';
    }).join("") || '<div class="empty">想到什么，就说给我听。</div>';
    if(html!==S.signature){$("#messages").innerHTML=html;S.signature=html;thread.scrollTop=bottom||stick?thread.scrollHeight:top;}
    $("#older").hidden=S.older===null;
    const options=Object.values(S.reviews).filter(r=>active(r.status));
    if(!S.targetChosen)S.target=options.at(-1)?.task_id || "";
    if(S.target&&!options.some(r=>r.task_id===S.target))S.target="";
    $("#target-row").hidden=!options.length;
    const targetHtml='<option value="">新消息</option>'+options.map(r=>'<option value="'+esc(r.task_id)+'" '+(r.task_id===S.target?"selected":"")+'>'+esc((r.status==="AwaitingInput"?"补充信息：":"追加要求：")+r.title.slice(0,40))+'</option>').join("");
    if(targetHtml!==S.targetSignature){$("#target").innerHTML=targetHtml;S.targetSignature=targetHtml;}
    $("#input").placeholder=S.target?"正在处理，可补充你的要求。":"想到什么，就说给我听。";
    $("#input").disabled=!S.loaded;
    $("#send").disabled=!S.loaded||S.busy||!!S.pending||!!S.conversation?.archived_at;
    $("#input").readOnly=!!S.conversation?.archived_at;
    updateBottom();
}
function updateBottom(){const t=$("#thread");$("#bottom").hidden=t.scrollHeight-t.clientHeight-t.scrollTop<160;}
function showPending(){
    const box=$("#pending");box.hidden=!S.pending;
    box.innerHTML=S.pending?'有一条操作尚未确认受理，请查询状态后使用原命令重试。<br><button id="retry">查询 / 重试原操作</button><button id="discard">放弃本地重试</button>':"";
    $("#send").disabled=!S.loaded||S.busy||!!S.pending;
}
async function perform(operation,parameters){
    if(S.busy||S.pending)return;
    S.pending={operation,parameters:{...parameters,command_id:id()},instance_id:S.session.instance_id,conversation_id:S.id};
    storage(pendingKey(),JSON.stringify(S.pending));await retry();
}
async function retry(){
    if(!S.pending||S.busy)return;
    S.busy=true;showPending();render();
    const pending=S.pending;
    try{
        if(pending.instance_id!==S.session.instance_id)throw new Error("后台已切换，不能重发旧请求。");
        const status=await rpc("command_status",{command_id:pending.parameters.command_id});
        const result=status.accepted?status:await rpc(pending.operation,pending.parameters);
        S.pending=null;storage(pendingKey(),null);
        if(result.accepted===false){const error=new Error(result.reason || "操作未被受理，输入已保留。");error.code="Rejected";throw error;}
        if(pending.operation==="submit")S.targetChosen=false;
        if(["submit","task_append_requirement","task_answer"].includes(pending.operation)&&pending.conversation_id===S.id){
            const sent=pending.parameters.request||pending.parameters.content;
            if($("#input").value===sent){$("#input").value="";storage(draftKey(),"");}
        }
        if(pending.operation==="conversation_create"){
            await directory();await selectConversation(result.conversation_id||result.result_identity||result.id);
        }else await refresh();
        notice(pending.operation==="submit"?"消息已受理。":"操作已受理。");
    }catch(error){
        errorMessage(error);
        if(["Rejected","Conflict","Blocked","NotFound"].includes(error.code)){
            S.pending=null;storage(pendingKey(),null);notice(error.message);
            try{await refresh();}catch{}
        }
    }finally{S.busy=false;showPending();if(S.session)render();}
}
$("#pair-form").addEventListener("submit",async event=>{
    event.preventDefault();const button=event.target.querySelector("button");button.disabled=true;
    try{await request("/api/pair",Object.fromEntries(new FormData(event.target)));event.target.elements.code.value="";await connect();}
    catch(error){notice(error.message);}finally{button.disabled=false;}
});
$("#compose").addEventListener("submit",async event=>{
    event.preventDefault();const text=$("#input").value;if(!text.trim()||!S.session||S.epoch===null)return;
    if(new TextEncoder().encode(text).length>60000){notice("消息过长，请拆分后发送。");return;}
    const target=S.reviews[S.target];
    if(target&&new TextEncoder().encode(text).length>8192){notice("追加要求最多 8 KiB，请拆分后发送。");return;}
    if(target)await perform(target.status==="AwaitingInput"?"task_answer":"task_append_requirement",{task_id:target.task_id,content:text});
    else await perform("submit",{request:text,conversation_id:S.id,expected_conversation_epoch:S.epoch});
});
$("#input").addEventListener("compositionstart",()=>composing=true);
$("#input").addEventListener("compositionend",()=>composing=false);
$("#input").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing&&!composing){event.preventDefault();$("#compose").requestSubmit();}});
$("#input").addEventListener("input",()=>{if(S.session&&S.id)storage(draftKey(),$("#input").value);});
$("#target").addEventListener("change",()=>{S.target=$("#target").value;S.targetChosen=true;render();});
$("#thread").addEventListener("scroll",updateBottom,{passive:true});
$("#target").addEventListener("wheel",event=>{event.preventDefault();$("#thread").scrollTop+=event.deltaY;},{passive:false});
$("#bottom").addEventListener("click",()=>{$("#thread").scrollTop=$("#thread").scrollHeight;});
$("#directory-button").addEventListener("click",()=>{$("#directory").hidden=!$("#directory").hidden;});
$("#older").addEventListener("click",async()=>{
    const button=$("#older"),generation=S.generation;button.disabled=true;
    try{const data=await rpc("conversation_timeline",{id:S.id,before:S.older,limit:50});if(generation!==S.generation)return;
        const thread=$("#thread"),height=thread.scrollHeight,top=thread.scrollTop;absorb(data,true);render();thread.scrollTop=top+thread.scrollHeight-height;
    }catch(error){errorMessage(error);}finally{button.disabled=false;}
});
$("#directory").addEventListener("click",async event=>{
    const button=event.target.closest("button");if(!button)return;
    try{if(button.dataset.session)await selectConversation(button.dataset.session);
        else if(button.dataset.more)await directory(true);
        else if(button.dataset.new){const title=prompt("新会话的名称");if(title?.trim())await perform("conversation_create",{title:title.trim()});}
    }catch(error){errorMessage(error);}
});
$("#messages").addEventListener("change",event=>{
    const {grant,task}=event.target.dataset;if(!grant)return;
    S.grants[task]=event.target.checked?[...new Set([...(S.grants[task]||[]),grant])]:(S.grants[task]||[]).filter(key=>key!==grant);render();
});
$("#messages").addEventListener("click",async event=>{
    const button=event.target.closest("button");if(!button)return;
    const {task,action,detail,full}=button.dataset;
    try{
        const generation=S.generation;
        if(detail){const review=await rpc("task",{task_id:detail});if(generation!==S.generation)return;$("#detail-body").innerHTML=(review.attempts||[]).map((attempt,index)=>'<h3>'+esc((index+1)+". "+(attempt.display_name||"工具操作"))+'</h3><p>'+esc(attempt.status==="Succeeded"?"已完成":attempt.status==="Failed"?"未完成":"执行状态待确认")+'</p>'+fields(attempt.result?.output??attempt.result?.error)).join("")||'<p>这项任务还没有工具执行记录。</p>';$("#detail").showModal();}
        if(full){const item=S.items.find(item=>item.id===full);if(!item)return;let offset=0,text="",revision=null;
            for(let page=0;page<16;page++){const data=await rpc("conversation_item",{id:S.id,item_id:full,offset,limit:65536});if(data.redacted_at||revision!==null&&revision!==data.revision)throw new Error("记录已经变化，请重新读取。");revision=data.revision;text+=data.body;offset+=data.body.length;if(!data.has_more)break;}
            if(generation!==S.generation)return;$("#detail-body").textContent=text;$("#detail").showModal();}
        if(action){const r=S.reviews[task];if(!r||(S.pending||S.busy))return;
            const parameters={task_id:task};if(action==="confirm")Object.assign(parameters,{expected_revision:r.task_revision,plan_hash:r.plan_hash,ordinary_tools:r.missing_permissions.ordinary_tools,high_risk_tools:(S.grants[task]||[])});
            await perform(action,parameters);}
    }catch(error){errorMessage(error);}
});
$("#pending").addEventListener("click",event=>{if(event.target.id==="retry")retry();if(event.target.id==="discard"&&confirm("只放弃本地重试，不会取消已受理的后台任务。继续？")){S.pending=null;storage(pendingKey(),null);showPending();render();}});
$("#close-detail").addEventListener("click",()=>$("#detail").close());
$("#logout").addEventListener("click",async()=>{try{await request("/api/logout",{});try{for(const key of Object.keys(sessionStorage))if(key.startsWith("deskpal-"))sessionStorage.removeItem(key);}catch{}S.pending=null;login();}catch(error){notice(error.message);}});
async function poll(){
    if(S.polling||document.hidden||S.busy)return;S.polling=true;
    try{if(!S.session||Date.now()-S.lastSession>15000)await connect();else{if(Date.now()-S.lastDirectory>10000)await directory();await refresh();}}
    catch(error){errorMessage(error);}finally{S.polling=false;}
}
document.addEventListener("visibilitychange",()=>{if(!document.hidden){S.lastSession=0;poll();}});
window.addEventListener("online",()=>{S.lastSession=0;poll();});
function fitViewport(){
    const viewport=window.visualViewport;
    if(!viewport||Math.abs(viewport.scale-1)>.05)return;
    // 软键盘改变可视高度时，让输入区留在可见范围；不改写用户输入或滚动锚点。
    $("#app").style.height=Math.round(viewport.height)+"px";
    updateBottom();
}
window.visualViewport?.addEventListener("resize",fitViewport);
fitViewport();
setInterval(poll,2500);poll();
