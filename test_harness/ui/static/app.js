"use strict";
let state = null;
let csrf = "";
let selectedArtifact = "";
let settingsInitialized = false;
let lastJobError = "";
const $ = (id) => document.getElementById(id);

function toast(message, error=false){const el=$("toast");el.textContent=message;el.className="toast show"+(error?" error":"");setTimeout(()=>el.className="toast",3500)}
async function request(path, options={}){const response=await fetch(path,options);const value=await response.json();if(!response.ok||!value.ok)throw new Error(value.error||`HTTP ${response.status}`);return value}
async function post(path,payload){return request(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(payload)})}
function text(tag,value,className){const el=document.createElement(tag);el.textContent=value;if(className)el.className=className;return el}

function renderStages(items){const root=$("stages");root.replaceChildren();items.forEach((item,index)=>{const li=document.createElement("li");li.className=`stage ${item.status}`;li.append(text("span",item.status==="done"?"✓":String(index+1),"stage-number"),text("h3",item.title),text("p",item.detail));root.append(li)})}
function renderReadiness(items){const root=$("readiness");root.replaceChildren();items.forEach(item=>{const li=document.createElement("li");li.className=item.ok?"ok":"";const dot=text("span","","check-dot");const body=document.createElement("div");body.append(text("div",item.label,"check-label"),text("div",item.detail,"check-detail"));li.append(dot,body);root.append(li)})}
function renderEvents(items){const root=$("events");root.replaceChildren();[...items].reverse().forEach(item=>{const li=document.createElement("li");li.append(text("strong",`${String(item.sequence||"").padStart(3,"0")} · ${item.type}`),text("span",item.timestamp||""));root.append(li)});if(!items.length)root.append(text("li","尚无事件"))}
function renderArtifacts(items){const root=$("artifactList");root.replaceChildren();$("artifactCount").textContent=`${items.length} 个可预览文件`;items.forEach(item=>{const button=text("button",item.path,"artifact-item"+(item.path===selectedArtifact?" active":""));button.type="button";button.title=`${item.bytes} bytes`;button.addEventListener("click",()=>loadArtifact(item.path));root.append(button)});if(!items.length)root.append(text("div","会话开始后，输出文件将在这里出现。","muted-text"));if(!selectedArtifact&&items.length){const preferred=[...items].reverse().find(item=>item.suffix===".md")||items[items.length-1];selectedArtifact=preferred.path;queueMicrotask(()=>loadArtifact(preferred.path))}}
function fillSettings(settings){const form=$("settingsForm");for(const [name,value] of Object.entries(settings)){const input=form.elements.namedItem(name);if(input)input.value=value??""}}
function render(next){state=next;csrf=next.csrf_token;const session=next.session;$("taskTitle").textContent=session.public_function||"等待输入公开接口";$("taskMeta").textContent=session.session_id?`会话 ${session.session_id} · 第 ${session.current_round} 轮`:"配置内网 Message API 与 SDK 后即可开始。";$("sessionBadge").textContent=session.state||"idle";$("jobBadge").textContent=next.job.status==="running"?`${next.job.operation} 运行中`:next.job.status;$("jobBadge").className="badge"+(next.job.status==="running"?"":" muted");renderStages(next.stages);renderReadiness(next.readiness);renderEvents(next.events);renderArtifacts(next.artifacts);if(!settingsInitialized){fillSettings(next.settings);settingsInitialized=true}const busy=next.job.status==="running";document.querySelectorAll("button").forEach(button=>{if(button.id!=="refreshButton"&&button.id!=="settingsToggle")button.disabled=busy});if(next.job.status==="failed"&&next.job.error&&next.job.error!==lastJobError){lastJobError=next.job.error;toast(next.job.error,true)}if(next.job.status!=="failed")lastJobError=""}
async function refresh(){try{render(await request("/api/state"))}catch(error){toast(error.message,true)}}
async function loadArtifact(path){try{const value=await request(`/api/artifact?path=${encodeURIComponent(path)}`);selectedArtifact=path;$("previewPath").textContent=path;$("artifactPreview").textContent=value.content;renderArtifacts(state.artifacts)}catch(error){toast(error.message,true)}}
async function action(path,payload,message){try{await post(path,payload);toast(message);await refresh()}catch(error){toast(error.message,true)}}

$("startForm").addEventListener("submit",event=>{event.preventDefault();action("/api/start",{public_function:$("publicFunction").value.trim()},"已开始生成，页面会自动更新")});
$("commentForm").addEventListener("submit",event=>{event.preventDefault();action("/api/comment",{comment:$("comment").value.trim()},"审查意见已提交")});
$("approveButton").addEventListener("click",()=>action("/api/approve",{},"已批准，开始 SDK 实测"));
$("retryButton").addEventListener("click",()=>action("/api/retry",{},"已提交重试"));
$("buildButton").addEventListener("click",()=>action("/api/build",{},"已开始构建 Runner"));
$("refreshButton").addEventListener("click",refresh);
$("settingsToggle").addEventListener("click",()=>$("settingsPanel").classList.toggle("hidden"));
$("settingsForm").addEventListener("submit",async event=>{event.preventDefault();const form=new FormData(event.currentTarget);const numeric=new Set(["candidate_count","candidate_parallelism","jobs","execution_timeout_seconds"]);const settings={profile:"intranet",thinking_mode:"omit"};for(const [key,value] of form.entries()){if(key!=="api_key")settings[key]=numeric.has(key)?Number(value):String(value)}await action("/api/settings",{settings,api_key:String(form.get("api_key")||"")},"配置已保存到本机")});
refresh();setInterval(refresh,2000);
