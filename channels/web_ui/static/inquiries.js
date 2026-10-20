"use strict";
const $=s=>document.querySelector(s), labels={extracting:"抽取询盘",needs_message_review:"待确认邮件意图",waiting_customer_info:"等待客户补充",waiting_rfq_review:"待核实询盘",researching:"正在背调",waiting_due_diligence_review:"待审批背调",preparing_quote:"回复 Agent 查询产品",waiting_negotiation:"背调已通过 · 待录入报价",calculating:"报价工具计算中",composing_reply:"回复 Agent 拟写邮件",waiting_quote_approval:"待确认发送",send_queued:"回复等待发送",clarify_queued:"补充邮件等待发送",sending:"邮件工具发送中",waiting_customer_feedback:"等待客户回复",waiting_acceptance_review:"待核实客户同意",accepted:"客户已同意并归档",closed:"已关闭",retry_pending:"执行失败，待处理",delivery_unknown:"发送结果待核对"};
const requestedCase=new URLSearchParams(location.search).get("case");
let current=/^INQ_[0-9a-f]{32}$/i.test(requestedCase||"")?requestedCase:null, detail=null, busy=false, cases=[], stageFilter="all", activeModule="inquiries", supportItems=[], currentHandoff=null, knowledgeRefreshTimer=null, workflowRefreshTimer=null, demoFixed=false, demoOnly=false, demoLiveMailbox=false;
const selectedCaseIds=new Set();
const stages=[
  {id:"new",label:"新询盘",states:["extracting","needs_message_review"]},
  {id:"research",label:"背调 / 审批",states:["researching","waiting_due_diligence_review"]},
  {id:"information",label:"待补资料",states:["waiting_customer_info","waiting_rfq_review"]},
  {id:"quote",label:"询盘回复",states:["preparing_quote","waiting_negotiation","calculating","composing_reply","waiting_quote_approval"]},
  {id:"reply",label:"待回复 / 跟进",states:["send_queued","clarify_queued","sending","waiting_customer_feedback","waiting_acceptance_review","delivery_unknown","retry_pending"]},
  {id:"complete",label:"已完成",states:["accepted","closed"]},
];
const stageOf=state=>stages.find(stage=>stage.states.includes(state))?.id||"reply";
const nextAction=state=>({extracting:"等待抽取采购需求",needs_message_review:"确认客户消息意图",waiting_customer_info:"核实或请求补充资料",waiting_rfq_review:"核实采购需求",researching:"等待背调结果",waiting_due_diligence_review:"审核背调结论",preparing_quote:"等待回复 Agent 查询产品",waiting_negotiation:"录入最终销售条件",calculating:"等待报价计算",composing_reply:"等待回复 Agent 生成报价邮件",waiting_quote_approval:"审核邮件与报价后发送",send_queued:"等待邮件工具发送",clarify_queued:"等待发送补充问题",sending:"核对发送进度",waiting_customer_feedback:"按计划跟进或处理客户回复",waiting_acceptance_review:"核实客户同意",accepted:"已归档",closed:"已关闭",retry_pending:"处理失败并重试",delivery_unknown:"核对邮件发送结果"})[state]||"查看详情";
function el(tag,text,parent,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;if(parent)parent.append(n);return n;}
function notice(text){const target=activeModule==="support"?"#supportNotice":activeModule==="system"?"#systemNotice":activeModule==="fallback"?"#fallbackNotice":"#notice";$(target).textContent=text;}
async function api(url,options={}){const r=await fetch(url,{credentials:"same-origin",...options}),text=await r.text();let data={};if(text){try{data=JSON.parse(text);}catch{data={detail:text.slice(0,300)};}}if(!r.ok)throw Error(r.status===401?"请先返回工作空间登录。":r.status===403?"当前账号没有此操作权限。":r.status===409?`进度已变化或当前阶段不允许操作，请刷新。(${data.detail||r.status})`:`操作未完成：${data.detail||r.status}`);return data;}
function button(parent,text,fn){const b=el("button",text,parent);b.type="button";b.onclick=()=>run(fn);return b;}
async function run(fn,successMessage="已更新。请根据当前阶段继续处理。"){if(busy)return;busy=true;document.querySelectorAll("button").forEach(b=>b.disabled=true);try{await fn();notice(successMessage);}catch(e){notice(e.message);}finally{busy=false;document.querySelectorAll("button").forEach(b=>b.disabled=false);syncCaseSelection();if(activeModule==="fallback")renderFallback();}}
async function act(action,payload={}){try{await api(`/api/inquiries/${current}/actions`,{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":crypto.randomUUID()},body:JSON.stringify({action,payload,expected_version:detail.case.version})});}catch(error){if(error.message.includes("进度已变化")){await refresh();throw Error("后台已更新询盘状态。页面已刷新，请核对邮件后再点一次发送。");}throw error;}await refresh();}
function field(parent,title,value="",type="text"){const l=el("label",title,parent), i=el(type==="textarea"?"textarea":"input",undefined,l);if(type!=="textarea")i.type=type;i.value=value??"";return i;}
function defaultQuoteValidUntil(){const date=new Date(Date.now()+30*24*60*60*1000),offset=-date.getTimezoneOffset(),sign=offset>=0?"+":"-",absolute=Math.abs(offset),local=new Date(date.getTime()+offset*60*1000).toISOString().slice(0,19);return `${local}${sign}${String(Math.floor(absolute/60)).padStart(2,"0")}:${String(absolute%60).padStart(2,"0")}`;}
function localDateTimeValue(value){const date=value?new Date(value):new Date(Date.now()+3*24*60*60*1000);if(Number.isNaN(date.getTime()))return"";const shifted=new Date(date.getTime()-date.getTimezoneOffset()*60000);return shifted.toISOString().slice(0,16);}
function select(parent,title,options,value){const l=el("label",title,parent),s=el("select",undefined,l);for(const [v,t]of options){const o=el("option",t,s);o.value=v;}s.value=value;return s;}
function table(parent,headers,rows){const t=el("table",undefined,parent),h=el("tr",undefined,t);headers.forEach(x=>el("th",x,h));rows.forEach(row=>{const r=el("tr",undefined,t);row.forEach(x=>el("td",String(x??""),r));});return t;}
function knowledgeState(item){if(item.index_status==="failed"||item.parse_status==="failed"||item.chunk_status==="failed")return["处理失败","failed"];if(item.index_status==="indexed")return["已完成索引",""];if(item.status==="review_required")return["等待复核","processing"];return["处理中","processing"];}
const knowledgeTypeLabel={product_info:"产品信息",product_manual:"产品手册",maintenance_manual:"产品维护手册",warranty_policy:"保修政策",faq:"常见问题",datasheet:"产品信息",product_document:"产品信息",after_sales_guide:"产品维护手册"};
const canonicalKnowledgeType=value=>({datasheet:"product_info",product_document:"product_info",after_sales_guide:"maintenance_manual"}[value]||value||"product_info");
function knowledgeMetadataEditor(card,item){const details=el("details",undefined,card,"knowledge-metadata-editor"),summary=el("summary","编辑文档信息",details),form=el("div",undefined,details,"knowledge-metadata-form");summary.title="修正产品型号、资料类别和版本后会自动重建索引";const type=select(form,"资料类别",[["product_info","产品信息（参数、规格、功能）"],["product_manual","产品手册（安装、配置、使用）"],["maintenance_manual","产品维护手册（故障、维修、保养）"],["warranty_policy","保修政策"],["faq","常见问题"]],canonicalKnowledgeType(item.document_type));const models=field(form,"适用产品型号（多个型号用逗号分隔）",(item.product_models||[]).join(", "));const version=field(form,"文档版本",item.document_version||String(item.version||1));const key=field(form,"文档标识",item.logical_document_key||"");button(form,"保存并重新建立索引",async()=>{if(!models.value.trim())throw Error("请填写这份资料适用的产品型号。");if(!version.value.trim())throw Error("请填写文档版本。");await api(`/api/knowledge/documents/${encodeURIComponent(item.document_id)}/metadata`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({document_type:type.value,product_models:models.value,document_version:version.value,logical_document_key:key.value})});$("#knowledgeNotice").textContent="文档信息已保存，正在按正确的型号和资料类别重建索引。";await loadKnowledge();});}
function renderKnowledge(data){const summary=data.summary||{},stats=$("#knowledgeStats"),list=$("#knowledgeList");stats.replaceChildren();for(const [label,value]of [["文档",summary.total_documents||0],["父块",summary.parent_count||0],["子块",summary.child_count||0],["已入索引",summary.indexed_count||0],["当前失败",summary.failed_document_count||0]]){const card=el("div",undefined,stats,"knowledge-stat");el("strong",String(value),card);el("span",label,card);}list.replaceChildren();for(const item of data.items||[]){const card=el("article",undefined,list,"knowledge-document"),head=el("div",undefined,card);el("h3",item.original_name||item.title||item.document_id,head);el("small",`${item.classification==="public"?"公开资料":"内部资料"} · ${knowledgeTypeLabel[item.document_type]||"类型待确认"}`,head);el("small",`适用型号：${(item.product_models||[]).join("、")||"未填写"} · 文档版本：${item.document_version||"未填写"}`,head);const[state,stateClass]=knowledgeState(item);const badge=el("span",state,card,`knowledge-status ${stateClass}`.trim());badge.title=item.last_error_code||"";for(const [label,value]of [["解析",item.parse_status||"—"],["分块",item.chunk_status||"—"],["子块数",item.child_count??0]]){const box=el("div",undefined,card);el("dt",label,box);el("dd",String(value),box);}knowledgeMetadataEditor(card,item);}if(!(data.items||[]).length)el("div","知识库还没有文件。点击“导入文件”开始建立 RAG 知识库。",list,"knowledge-empty");}
function scheduleKnowledgeRefresh(items){clearTimeout(knowledgeRefreshTimer);knowledgeRefreshTimer=null;const processing=(items||[]).some(item=>knowledgeState(item)[1]==="processing");if(activeModule!=="knowledge"||document.hidden||!processing)return;knowledgeRefreshTimer=setTimeout(()=>loadKnowledge(),2000);}
async function loadKnowledge(){const search=$("#knowledgeSearch").value.trim();$("#knowledgeNotice").textContent="正在读取知识库…";try{const data=await api(`/api/knowledge/documents?limit=100&search=${encodeURIComponent(search)}`);renderKnowledge(data);$("#knowledgeNotice").textContent=`共 ${data.total||0} 份有效文档。`;scheduleKnowledgeRefresh(data.items);}catch(e){$("#knowledgeNotice").textContent=`知识库读取失败：${e.message}`;}}
async function importKnowledge(file){if(!file)return;$("#knowledgeNotice").textContent=`正在导入“${file.name}”… PDF、图片和大文件可能需要几分钟。`;const response=await fetch("/api/knowledge/import",{method:"POST",credentials:"same-origin",headers:{"Content-Type":file.type||"application/octet-stream","X-File-Name":encodeURIComponent(file.name),"X-Knowledge-Classification":$("#knowledgeClassification").value},body:file});const data=await response.json().catch(()=>({detail:`HTTP ${response.status}`}));if(!response.ok)throw Error(data.detail||`HTTP ${response.status}`);$("#knowledgeNotice").textContent=data.duplicate?`“${file.name}”已存在，已继续检查处理状态。`:`“${file.name}”已接收，正在清洗、分块并建立索引。`;await loadKnowledge();}
function systemRows(root,rows){root.replaceChildren();for(const [label,value]of rows){const box=el("div",undefined,root);el("dt",label,box);el("dd",String(value??"—"),box);}}
async function statusApi(url){const response=await fetch(url,{credentials:"same-origin"});let data={};try{data=await response.json();}catch{}return{ok:response.ok,status:response.status,data};}
async function loadSystem(){
  $("#systemNotice").textContent="正在检查系统状态…";
  try{
    const[health,ready,metrics,knowledge,ui]=await Promise.all([statusApi("/healthz"),statusApi("/readyz"),statusApi("/api/rag/metrics"),statusApi("/api/knowledge/documents?limit=1"),statusApi("/api/ui/config")]);
    const summary=knowledge.data.summary||{},vector=metrics.data||{},stats=$("#systemStats");stats.replaceChildren();
    for(const[label,value,state]of[["内部工作台",health.ok?"运行中":"异常",health.ok?"ready":"failed"],["RAG 检索",ready.ok?"可用":"不可用",ready.ok?"ready":"failed"],["知识库文档",summary.total_documents??0,"ready"],["处理失败",summary.failed_document_count??0,(summary.failed_document_count||0)>0?"failed":"ready"]]){const card=el("div",undefined,stats,`system-stat ${state}`);el("span",label,card);el("strong",String(value),card);}
    const customerUrl=ui.data.customer_portal_url||"未启用";
    systemRows($("#systemEndpoints"),[["客户智能客服",customerUrl],["企业内部工作台",location.origin],["运行模式",ui.data.demo_mode?(ui.data.demo_fixed_data?"固定演示数据":"真实接口优先，失败降级"):"正常模式"]]);
    systemRows($("#systemRag"),[["就绪状态",ready.data.status||"不可用"],["向量数据库",vector.backend||"未知"],["向量服务",vector.ready?"可用":"不可用"],["最近成功检查",vector.last_success_at||"—"]]);
    systemRows($("#systemKnowledge"),[["有效文档",summary.total_documents??0],["父块",summary.parent_count??0],["子块",summary.child_count??0],["已建立索引",summary.indexed_count??0],["失败文档",summary.failed_document_count??0]]);
    $("#systemNotice").textContent=health.ok&&ready.ok?"系统运行正常。":"部分服务未就绪，请查看下方状态。";
  }catch(error){$("#systemNotice").textContent=`系统状态读取失败：${error.message}`;}
}
function switchModule(name){activeModule=["knowledge","support","fallback","system"].includes(name)?name:"inquiries";clearTimeout(knowledgeRefreshTimer);knowledgeRefreshTimer=null;clearTimeout(workflowRefreshTimer);workflowRefreshTimer=null;$("#inquiriesModule").hidden=activeModule!=="inquiries";$("#supportModule").hidden=activeModule!=="support";$("#knowledgeModule").hidden=activeModule!=="knowledge";$("#fallbackModule").hidden=activeModule!=="fallback";$("#systemModule").hidden=activeModule!=="system";document.querySelectorAll("[data-module]").forEach(button=>button.classList.toggle("active",button.dataset.module===activeModule));if(activeModule==="knowledge")loadKnowledge();if(activeModule==="support")loadSupport();if(activeModule==="fallback")renderFallback();if(activeModule==="system")loadSystem();if(activeModule==="inquiries")scheduleWorkflowRefresh();}
const fallbackStorageKey="nanoclaw_demo_fallback_templates_v1";
const fallbackTemplates={
  official_website:{label:"公司官网",body:"【演示备用｜公司官网】官网查询接口暂不可用。本次暂不确认公司业务、产品、政策或联系方式；请在接口恢复后核对官网原文与网址。"},
  map_lookup:{label:"地图地点",body:"【演示备用｜地图地点】地图查询接口暂不可用。公司地址与实际经营地点目前未核验，背调报告中的地点信息应保持待核实。"},
  customs_lookup:{label:"海关数据",body:"【演示备用｜海关数据】海关数据接口暂不可用。本次无法确认进出口记录、交易频次或贸易伙伴；请勿把缺失数据解读为没有交易。"},
  news_search:{label:"新闻搜索",body:"【演示备用｜新闻搜索】新闻搜索接口暂不可用。本次不对近期新闻或风险事件作出结论，待恢复后按发布日期和来源重新核查。"},
  report_generation:{label:"背调报告生成",body:"【演示备用｜报告生成】报告模型暂不可用。当前只展示已保存的原始询盘和已有调查步骤；报告结论与公司核验结果待模型恢复后重新生成、人工审批。"},
  product_catalog:{label:"产品与报价工具",body:"【演示备用｜产品与报价】产品目录或报价工具暂不可用。产品规格、库存、交期与价格均需销售人员核实；此说明不构成正式报价。"},
  email_delivery:{label:"邮件发送",body:"【演示备用｜邮件发送】邮件工具暂不可用。回复草稿可在工作台查看和复制，发送状态仍为未确认；请人工核对发件箱后再决定是否发送。"},
  other:{label:"其他接口",body:"【演示备用｜接口异常】当前外部接口暂不可用。本次展示只说明已保存的流程和数据，未核实的业务事实保持待核实，恢复后补查并记录来源。"}
};
function loadFallbackDrafts(){try{const saved=JSON.parse(localStorage.getItem(fallbackStorageKey)||"{}");if(!saved||typeof saved!=="object"||Array.isArray(saved))return{};return Object.fromEntries(Object.keys(fallbackTemplates).filter(key=>typeof saved[key]==="string").map(key=>[key,saved[key].slice(0,4000)]));}catch{return{};}}
let fallbackDrafts=loadFallbackDrafts();
function fallbackBody(category){return fallbackDrafts[category]??fallbackTemplates[category]?.body??"";}
function saveFallbackDraft(){const category=$("#fallbackCategory").value;fallbackDrafts[category]=$("#fallbackBody").value.slice(0,4000);try{localStorage.setItem(fallbackStorageKey,JSON.stringify(fallbackDrafts));$("#fallbackNotice").textContent="已保存在当前浏览器。";}catch{$("#fallbackNotice").textContent="浏览器禁止本机保存；请复制备用内容。";}}
function renderDemoFallbackNotes(root){const notes=(detail?.artifacts||[]).filter(item=>item.kind==="demo_fallback_note").sort((a,b)=>(b.payload?.created_at||0)-(a.payload?.created_at||0));if(!notes.length)return false;const section=el("section",undefined,root,"demo-fallback-notes");el("h3","人工演示说明 · 非背调证据",section);for(const note of notes){const box=el("div",undefined,section,"demo-fallback-note");const category=note.payload?.category;el("strong",`${fallbackTemplates[category]?.label||"其他接口"} · ${new Date((note.payload?.created_at||0)*1000).toLocaleString()} · ${note.payload?.created_by||"人工录入"}`,box);el("p",note.payload?.body||"",box);}return true;}
function renderFallback(){const category=$("#fallbackCategory").value;$("#fallbackBody").value=fallbackBody(category);const selected=detail?.case?.inquiry_id===current?detail.case:null;$("#fallbackCase").textContent=selected?`当前询盘：${selected.data?.rfq?.customer?.company||selected.data?.rfq?.customer?.name||selected.inquiry_id} · ${selected.inquiry_id} · ${labels[selected.state]||selected.state}`:"尚未选择询盘；模板仍可编辑和复制。";$("#fallbackAttach").disabled=!selected||selected.data?.execution_mode!=="demo"||!["waiting_customer_info","waiting_rfq_review","waiting_due_diligence_review","waiting_negotiation","waiting_quote_approval","waiting_customer_feedback","retry_pending","delivery_unknown"].includes(selected.state);const notes=$("#fallbackAttachedNotes");notes.replaceChildren();if(selected&&!renderDemoFallbackNotes(notes))el("p","当前询盘尚无备用说明。",notes,"muted");}
const supportStatusLabel={pending:"待接单",assigned:"处理中",replied:"已回复",completed:"已完成"};
function renderSupportDetail(record){
  const root=$("#supportDetail");root.replaceChildren();
  if(!record){root.innerHTML='<div class="empty-detail"><strong>选择一条工单</strong><p>这里会显示客户问题、会话摘要和回复操作。</p></div>';return;}
  const head=el("article",undefined,root);el("span",supportStatusLabel[record.status]||record.status,head,`support-state ${record.status}`);el("h2",record.product_model?`产品：${record.product_model}`:"客户咨询",head);el("p",`工单 ${record.handoff_id} · ${new Date(record.created_at).toLocaleString()}`,head,"muted");el("p",`转人工原因：${record.reason||"未说明"} · 处理人：${record.assigned_to||"尚未接单"}`,head,"next-action");const due=record.first_response_due_at?new Date(record.first_response_due_at).toLocaleString():"—";el("p",record.first_response_sla==="overdue"?`首响已超时（应在 ${due} 前回复）`:`首响时限：${due}`,head,record.first_response_sla==="overdue"?"support-overdue":"muted");
  const question=el("article",undefined,root);el("h3","客户当前问题",question);el("pre",record.question||"",question);if(record.summary){el("h3","前 15 轮摘要",question);el("p",record.summary,question);}if(record.recent_turns?.length){const recent=el("details",undefined,question);el("summary","查看最近完整对话",recent);for(const turn of record.recent_turns){const box=el("div",undefined,recent,"support-turn");el("strong",`客户：${turn.user||""}`,box);el("p",`机器人：${turn.assistant||""}`,box);}}
  const history=el("article",undefined,root);el("h3","人工处理记录",history);for(const update of record.updates||[]){const box=el("div",undefined,history,"support-update");el("strong",`${update.operator||"客服"} · ${new Date(update.created_at).toLocaleString()}`,box);el("p",update.type==="completed"?"已完成工单":update.message,box);}if(!(record.updates||[]).length)el("p","尚无人工回复。",history,"muted");
  if(record.status!=="completed"){const actions=el("article",undefined,root);el("h3","处理工单",actions);if(record.status==="pending")button(actions,"接单",async()=>{await api(`/api/customer-support/handoffs/${record.handoff_id}/claim`,{method:"POST"});await loadSupport(record.handoff_id);});const reply=field(actions,"回复客户","","textarea");button(actions,"发送回复",async()=>{if(!reply.value.trim())throw Error("请填写回复内容。");await api(`/api/customer-support/handoffs/${record.handoff_id}/reply`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:reply.value,complete:false})});await loadSupport(record.handoff_id);});button(actions,"回复并完成",async()=>{if(!reply.value.trim())throw Error("请填写回复内容。");await api(`/api/customer-support/handoffs/${record.handoff_id}/reply`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:reply.value,complete:true})});await loadSupport(record.handoff_id);});button(actions,"直接标记完成",async()=>{await api(`/api/customer-support/handoffs/${record.handoff_id}/complete`,{method:"POST"});await loadSupport(record.handoff_id);});}
}
function renderSupport(data){
  supportItems=data.items||[];const counts=data.counts||{},stats=$("#supportStats");stats.replaceChildren();for(const [label,value]of [["全部",data.total||0],["待接单",counts.pending||0],["已超时",counts.overdue||0],["处理中",counts.assigned||0],["已回复",counts.replied||0],["已完成",counts.completed||0]]){const card=el("div",undefined,stats,`knowledge-stat${label==="已超时"&&value?" failed":""}`);el("strong",String(value),card);el("span",label,card);}
  const list=$("#supportList");list.replaceChildren();for(const record of supportItems){const card=el("button",undefined,list,`support-ticket${currentHandoff===record.handoff_id?" selected":""}${record.first_response_sla==="overdue"?" overdue":""}`);card.type="button";const head=el("span",undefined,card,"support-ticket-head");el("strong",record.product_model||record.intent||"客户咨询",head);el("span",supportStatusLabel[record.status]||record.status,head,`support-state ${record.status}`);el("p",record.question||"",card);el("small",`${new Date(record.created_at).toLocaleString()} · ${record.assigned_to||"未接单"}${record.first_response_sla==="overdue"?" · 首响超时":""}`,card);card.onclick=()=>{currentHandoff=record.handoff_id;renderSupport(data);};}if(!supportItems.length)el("p","当前没有转人工工单。",list,"muted");const selected=supportItems.find(item=>item.handoff_id===currentHandoff)||supportItems[0];currentHandoff=selected?.handoff_id||null;renderSupportDetail(selected);
}
async function loadSupport(preferred){if(preferred)currentHandoff=preferred;$("#supportNotice").textContent="正在读取工单…";try{const status=$("#supportStatus").value;const data=await api(`/api/customer-support/handoffs?limit=200&status=${encodeURIComponent(status)}`);renderSupport(data);$("#supportNotice").textContent=`共 ${data.total||0} 条工单，当前显示 ${(data.items||[]).length} 条。`;}catch(e){$("#supportNotice").textContent=`工单读取失败：${e.message}`;}}
function rfqEditor(root,d){el("h2","核实客户及采购需求",root);if(d.research_status==="queued")el("p","企业背调正在后台执行；采购字段仍需补全后才能进入报价。",root,"muted");if(d.research_status==="failed")el("p",`企业背调失败：${d.research_error_code||d.error_code||"未知错误"}。修正配置后再次保存即可重试。`,root,"muted");if(d.clarification_reason)el("p",`回复 Agent 请求补充信息：${d.clarification_reason}。确认邮件内容后再发送。`,root,"next-action");if(d.research_status==="identity_rejected")el("p",`公司主体未通过自动核验。请补充公司法定名称、官网、注册地址或注册编号。未匹配项：${(d.identity_gaps||[]).join("、")||"未知"}`,root,"muted");const rfq=d.rfq||{customer:{},items:[]},c=rfq.customer||{},type=select(root,"客户类型",[["company","企业"],["individual","个人"]],d.customer_type==="individual"?"individual":"company"),grid=el("div",undefined,root,"grid");const name=field(grid,"联系人姓名",c.name),company=field(grid,"企业法定名称",c.company),website=field(grid,"官网",c.website),country=field(grid,"国家 / 地区",rfq.country),city=field(grid,"城市",c.city),address=field(grid,"注册地址",c.address),registration=field(grid,"企业注册编号",c.registration_number||c.registration_id),email=field(grid,"联系邮箱",c.email),phone=field(grid,"电话 / WhatsApp",c.phone||c.whatsapp);const items=el("div",undefined,root),edit=[];function add(row={}){const fs=el("fieldset",undefined,items),g=el("div",undefined,fs,"grid");const fields=[field(g,"产品 / 型号",row.product),field(g,"规格",row.specification),field(g,"数量",row.quantity,"number"),field(g,"单位",row.unit)];edit.push({fs,fields});button(fs,"移除此行",()=>{fs.remove();});} (rfq.items?.length?rfq.items:[{}]).forEach(add);button(root,"添加产品",()=>add());button(root,"保存并检查完整性",()=>act("review_rfq",{customer_type:type.value,rfq:{...rfq,customer:{...c,name:name.value,company:company.value,website:website.value,city:city.value,address:address.value,registration_number:registration.value,email:email.value,phone:phone.value},country:country.value,items:edit.filter(x=>x.fs.isConnected).map(({fields:f})=>({product:f[0].value,specification:f[1].value,quantity:f[2].value,unit:f[3].value}))}}));if(detail.case.state==="waiting_customer_info"){el("h3","发送补充信息请求",root);const msg=field(root,"确认邮件内容后发送",d.clarification_draft||"Hello, please provide the missing product details, quantity and units. Thank you.","textarea");el("p",`发送至：${d.recipient||"当前来源没有邮箱，请先线下补充并导入回复。"}`,root,"muted");button(root,"确认并发送补充邮件",()=>act("request_clarification",{body:msg.value}));}}
function negotiation(root,d){el("h2","填写数量、单价与交易条件",root);el("p","流程暂停在此等待销售人员填写。提交后先由报价计算工具核算金额，再由回复 Agent 根据客户来信语言拟写邮件。",root,"next-action");if(d.sales_handoff_reason)el("p",`回复 Agent 已转交销售复核：${d.sales_handoff_reason}。请核实并调整交易条件后重新生成草稿。`,root,"next-action");const plan=d.quote_preparation?.reply_plan;if(plan){el("p",`回复 Agent 计划：${plan.plan_id}`,root,"muted");table(root,["步骤","工具","状态"],(plan.steps||[]).map(s=>[s.id,s.tool,s.status]));}el("p","价格为最终销售单价。库存仅为查询结果，尚未预留。费用需明确填写，免收填 0。",root,"muted");for(const c of d.quote_preparation?.candidates||[]){el("h3",`需求：${c.requested.product}`,root);table(root,["SKU","名称","单位","库存","起订量","参考价格"],c.products.map(p=>[p.sku,p.name_en||p.name_cn,p.unit,p.inventory,p.moq,p.price_usd]));}const old=d.terms||{},grid=el("div",undefined,root,"grid"),f={};for(const [key,title,def]of [["recipient","报价接收邮箱",d.recipient],["outbound_account_id","发送邮箱编号",d.account_id],["currency","币种","USD"],["payment_terms","付款条件",""],["trade_term","贸易术语",""],["delivery","交货时间与地点",""],["valid_until","有效期（默认 30 天后，含时区）",defaultQuoteValidUntil()]])f[key]=field(grid,title,old[key]||def);const rows=el("div",undefined,root),ed=[];function add(line={}){const fs=el("fieldset",undefined,rows),g=el("div",undefined,fs,"grid"),a=[field(g,"确认的 SKU",line.sku),field(g,"数量",line.quantity,"number"),field(g,"单位",line.unit),field(g,"最终销售单价",line.unit_price,"number")];a[3].step="any";ed.push({fs,a});button(fs,"移除此行",()=>fs.remove());}(old.items?.length?old.items:[{}]).forEach(add);button(root,"添加报价产品",()=>add());const fees={};for(const [k,title]of [["freight","运费"],["packaging","包装费"],["tax","税费"],["other","其他费用"]]){fees[k]=field(grid,title,old.fees?.[k]??"","number");fees[k].step="any";}button(root,"提交并调用报价计算工具",()=>act("submit_terms",{terms:{...Object.fromEntries(Object.entries(f).map(([k,v])=>[k,v.value])),price_basis:"final_net_unit_price",fees:Object.fromEntries(Object.entries(fees).map(([k,v])=>[k,v.value])),items:ed.filter(x=>x.fs.isConnected).map(({a})=>({sku:a[0].value,quantity:a[1].value,unit:a[2].value,unit_price:a[3].value}))}}));}
function followUpPanel(root,d){
  const follow=d.follow_up||{},due=follow.due_at?new Date(follow.due_at):null,overdue=due&&due.getTime()<Date.now();
  el("h2","客户跟进",root);el("p",`报价发送时间：${d.sent_at?new Date(d.sent_at*1000).toLocaleString():"未记录"} · 已发送跟进 ${d.follow_up_count||0} 次`,root,"muted");
  el("p",due?`${overdue?"跟进已逾期":"下次跟进"}：${due.toLocaleString()} · 负责人：${follow.owner||"未分配"}`:"尚未设置下一次跟进时间。",root,overdue?"follow-up-overdue":"next-action");
  const plan=el("section",undefined,root,"follow-up-plan"),grid=el("div",undefined,plan,"grid"),owner=field(grid,"跟进负责人",follow.owner||"sales"),dueAt=field(grid,"下次跟进时间",localDateTimeValue(follow.due_at),"datetime-local"),note=field(plan,"跟进备注",follow.note||"","textarea");
  button(plan,"保存跟进计划",()=>{if(!dueAt.value)throw Error("请选择下一次跟进时间。");return act("schedule_follow_up",{owner:owner.value,due_at:new Date(dueAt.value).toISOString(),note:note.value});});
  const draft=el("section",undefined,root,"follow-up-draft");el("h3","生成跟进邮件",draft);el("p","回复 Agent 会沿用已发送报价、客户语言和邮件线程生成草稿；发送前仍需人工确认。",draft,"muted");const instruction=field(draft,"本次跟进重点（选填）","","textarea");button(draft,"生成跟进邮件草稿",()=>act("prepare_follow_up",{instruction:instruction.value}));
  const history=detail.artifacts.filter(a=>a.kind==="follow_up_schedule");if(history.length){const records=el("details",undefined,root);el("summary",`查看跟进计划记录（${history.length}）`,records);table(records,["更新时间","负责人","计划时间","备注"],history.map(a=>[new Date(a.created_at*1000).toLocaleString(),a.payload.owner||"—",a.payload.due_at?new Date(a.payload.due_at).toLocaleString():"—",a.payload.note||"—"]));}
}
const reportFieldLabels={company_identity:"企业主体名称",legal_name:"企业主体名称",company_overview:"企业业务",business_scope:"企业业务",products:"主要产品",product_info:"产品信息",company_policy:"企业公开政策",customs_activity:"海关货运记录",customs_data:"海关数据",shipment_activity:"货运记录",map_location:"企业地点",news_check:"新闻核查",news:"新闻信息",risk:"风险信息",contact:"联系方式",address:"公司地址",website:"公司官网",phone:"联系电话",email:"联系邮箱",other:"其他调查信息"};
const reportContactLabels={website:"官网",email:"邮箱",phone:"电话",address:"地址"};
const reportPersonLabels={name:"姓名",role:"职务",email:"邮箱",whatsapp:"WhatsApp",relationship_status:"与企业的代表关系"};
const reportToolLabels={official_website:"公司官网",map_lookup:"地图地点",customs_lookup:"海关数据",news_search:"新闻来源",customer_message:"客户原始消息",business_history:"内部历史记录"};
const reportStatusLabels={supported:"有独立证据支持",customer_provided:"客户自行提供",unverified:"尚待核实",conflict:"来源存在冲突",verified:"已核验",needs_review:"需要人工核验",rejected:"未通过核验",pending:"待核验",requires_human_confirmation:"需要人工确认",ok:"已完成",unavailable:"暂不可用",not_found:"未找到结果",identity_not_verified:"主体未核验"};
const reportQualityLabels={verified_page:"已读取网页原文",provider_extracted:"搜索服务提取的网页正文",trusted_api:"可信接口记录",directory_api:"公开目录记录",lead_only:"仅搜索线索",demo_fixture:"本地演示数据",customer_claim:"客户陈述",reviewed_internal:"内部记录",aggregate_data:"汇总数据",unverified:"未核实"};
const reportGapCodes={...reportToolLabels,...reportStatusLabels,domain:"官网域名",legal_name:"企业法定名称",country:"国家或地区",address:"公司地址",phone:"联系电话",registration_number:"企业注册编号",independent_source:"独立公开来源",company_identity_requires_review:"公司主体需要人工核验",search_not_configured:"搜索接口尚未配置",official_website_domain_not_confirmed:"未找到可确认属于该公司的官网域名",company_customs_provider_not_configured:"海关数据接口尚未配置",inquiry_mcp_invalid_response:"调查工具返回无法解析的结果",customs_account_unverified_or_unauthorized:"海关数据账号未通过验证或无权限",customs_credits_exhausted:"海关数据账号额度不足",customs_company_not_found:"未查到该公司的海关记录",demo_tool_not_supported:"演示工具不支持此查询"};
function reportLabel(value,labels){const raw=String(value??"").trim();return labels[raw]||raw;}
function reportGapLabel(value){return String(value||"").replace(/[a-z][a-z_]+/gi,part=>reportGapCodes[part]||part);}
function reportSourceUrl(value){try{const url=new URL(String(value||""));return ["https:","http:"].includes(url.protocol)?url.href:"";}catch{return"";}}
function reportSourceName(e){const type=reportToolLabels[e?.source_type]||"调查来源";const detail=e?.title||e?.search_title||e?.publisher||e?.displayName?.text||e?.matched_company_name||e?.record_id||e?.provider||"";return detail?`${type} · ${detail}`:type;}
function reportEvidenceRefs(root,ids,byId){const wrap=el("div",undefined,root,"report-source-links");el("small","来源：",wrap);const unique=[...new Set(Array.isArray(ids)?ids:[])];if(!unique.length){el("span","未提供可定位来源",wrap,"report-source-missing");return;}for(const id of unique){const e=byId.get(id);if(!e){el("span",`来源编号 ${id}（未找到原始记录）`,wrap,"report-source-missing");continue;}const text=`${reportSourceName(e)}${e.demo_data?"（演示数据）":""}`;const url=e.demo_data?"":reportSourceUrl(e.url);const item=url?el("a",text,wrap,"report-source-link"):el("span",text,wrap,"report-source-link");if(url){item.href=url;item.target="_blank";item.rel="noopener noreferrer";}item.title=`证据编号：${id}`;}}
function reportItem(root,title,value,status,ids,byId){const card=el("div",undefined,root,"report-item"),head=el("div",undefined,card,"report-item-head");el("strong",title||"调查结论",head);el("span",reportLabel(status,reportStatusLabels)||"尚待核实",head,"report-item-status");el("p",value||"未提供内容",card);reportEvidenceRefs(card,ids,byId);}
function reportView(root,p){
  el("h2","背调报告 · 中文审批版",root);
  el("p","以下按结论展示来源和核验状态；回复 Agent 使用已审批报告中的结构化事实与证据编号。",root,"muted");
  if(p.demo_data)el("p","演示模式：部分或全部外部调查结果来自本地演示数据，不能作为真实企业的核验依据。",root,"next-action");
  const identity=p.company_identity||{},evidence=Array.isArray(p.evidence)?p.evidence:[],byId=new Map(evidence.filter(e=>e?.evidence_id).map(e=>[e.evidence_id,e]));
  if(!evidence.some(e=>!['customer_message','business_history'].includes(e?.source_type)))el("p","这份报告没有取得任何外部调查来源。修复后需点击下方“提交补充调查”重新生成；当前结论不能用于审批判断。",root,"review-missing");
  el("p",`公司主体：${identity.legal_name||"未确认"} · 核验状态：${reportLabel(identity.status,reportStatusLabels)||"未知"} · 评分 ${identity.confidence??0}/100 · 官网域名 ${identity.official_domain||"未确认"}`,root,identity.status==="verified"?"next-action":"muted");
  el("h3","调查摘要",root);
  const summary=String(p.summary||"").trim();
  if(summary&&/[\u3400-\u9fff]/.test(summary)){el("p",summary,root,"report-summary");}
  else{el("p",`本次调查记录 ${(p.facts||[]).length} 条结论、${(p.gaps||[]).length} 项待核实信息。请逐项查看证据来源与核验状态。`,root,"report-summary");if(summary){const older=el("details",undefined,root);el("summary","查看旧版英文摘要",older);el("p",summary,older);}}
  el("h3","调查结论与对应来源",root);
  if(!(p.facts||[]).length)el("p","目前没有可展示的调查结论。",root,"muted");
  for(const fact of p.facts||[])reportItem(root,reportLabel(fact.field||fact.field_code,reportFieldLabels),fact.value, fact.status,fact.evidence_ids,byId);
  el("h3","企业公开联系方式",root);
  let contactCount=0;for(const [key,rows] of Object.entries(p.company_contacts||{})){for(const item of Array.isArray(rows)?rows:[]){contactCount++;reportItem(root,reportContactLabels[key]||"联系方式",item.value,item.status,item.evidence_ids,byId);}}
  if(!contactCount)el("p","尚未核实到企业公开联系方式。",root,"muted");
  const person=p.inquiry_contact||{};el("h3","询盘联系人",root);
  for(const [key,label] of Object.entries(reportPersonLabels)){const value=person[key];if(value)el("p",`${label}：${key==="relationship_status"?reportLabel(value,reportStatusLabels):value}`,root);}
  const gaps=[...(Array.isArray(p.gaps)?p.gaps:[]),...(Array.isArray(p.conflicts)?p.conflicts:[])];el("h3","待核实事项与冲突",root);
  if(gaps.length){const list=el("ul",undefined,root,"report-gaps");for(const gap of gaps)el("li",typeof gap==="string"?reportGapLabel(gap):"存在需要人工核对的结构化信息",list);}else el("p","当前未记录其他待核实事项。",root,"muted");
  const sources=el("section",undefined,root,"report-evidence");el("h3","来源清单",sources);
  if(!evidence.length)el("p","本次报告没有保存调查来源。",sources,"muted");
  for(const e of evidence){const box=el("div",undefined,sources,"report-evidence-card"),head=el("div",undefined,box,"report-item-head");el("strong",reportSourceName(e),head);el("span",reportQualityLabels[e.quality]||"待核实",head,"report-item-status");el("small",`证据编号：${e.evidence_id||"未编号"}${e.demo_data?" · 本地演示数据，非真实外部来源":""}`,box);const url=e.demo_data?"":reportSourceUrl(e.url);if(url){const link=el("a",url,box);link.href=url;link.target="_blank";link.rel="noopener noreferrer";}else if(e.provider||e.record_id||e.id){el("p",`来源记录：${[e.provider,e.record_id||e.id].filter(Boolean).join(" · ")}`,box,"muted");}const excerpt=String(e.text||e.content||"").trim();if(excerpt){const raw=el("details",undefined,box);el("summary","查看来源原文摘录（保留原语言）",raw);el("p",excerpt.slice(0,1200),raw);}}
  const originals=el("details",undefined,root,"report-originals");el("summary","查看客户原始邮件 / 消息（保留原语言）",originals);
  for(const m of p.original_messages||[]){const box=el("div",undefined,originals,"source");el("strong",`${m.subject||"无主题"} · ${m.sender||"未知发件人"}`,box);el("small",`来源编号：${m.message_id}${m.archive?" · 原件已归档":""}`,box);el("pre",m.text||"",box);}
  const plan=el("details",undefined,root);el("summary","查看调查计划与执行状态",plan);
  table(plan,["步骤","调查工具","执行状态","失败原因","来源数量"],(p.execution_log||[]).map(run=>[run.step_id,reportLabel(run.tool,reportToolLabels),reportLabel(run.status,reportStatusLabels)||"未执行",reportGapLabel(run.reason)||"—",(run.evidence_ids||[]).length]));
}
function dueDiligenceReview(root,report){
  root.classList.add("due-diligence-review");
  el("h2","审批背调报告",root);
  if(!report||!report.payload){
    el("p","当前询盘没有可读取的背调报告，无法审批。请刷新页面；若仍缺失，请重新执行背调。",root,"review-missing");
    renderDemoFallbackNotes(root);
    button(root,"打开演示备用信息",async()=>switchModule("fallback"));
    return;
  }
  const meta=el("p",`本次审批对应报告：${report.artifact_id} · 版本 ${report.version}`,root,"muted");
  meta.title="批准操作只对应本次背调生成的报告";
  reportView(el("section",undefined,root,"review-report"),report.payload);
  renderDemoFallbackNotes(root);
  button(root,"打开演示备用信息",async()=>switchModule("fallback"));
  const controls=el("section",undefined,root,"review-controls");
  el("h3","审批决定",controls);
  el("p","请核对公司主体、客户原邮件、报告来源和待核实项。通过后系统先查询产品目录，再暂停在报价录入阶段，等待销售填写数量、单价和交易条件。",controls,"muted");
  const unresolved=(report.payload.gaps||[]).length+(report.payload.conflicts||[]).length;
  if(unresolved)el("p",`本报告有 ${unresolved} 项待核实或冲突。若决定继续，必须填写审批依据；这表示接受这些风险进入报价准备，不表示相关信息已经核实。`,controls,"review-warning");
  const reason=field(controls,unresolved?"审批依据（必填）":"审批说明（选填）","","textarea");
  reason.placeholder=unresolved?"例如：已逐项阅读待核实内容，确认仅用于准备报价，发送前继续人工复核。":"可记录审批判断。";
  const error=el("p","",controls,"review-inline-error");error.hidden=true;
  button(controls,"通过背调并进入报价阶段",async()=>{
    if(unresolved&&!reason.value.trim()){error.textContent="请先填写审批依据，再进入报价阶段。";error.hidden=false;reason.focus();throw Error(error.textContent);}
    error.hidden=true;
    try{await act("approve_due_diligence",{reason:reason.value});}
    catch(e){error.textContent=e.message.includes("unverified_items_require_reason")?"这份报告存在待核实项，请填写审批依据后再提交。":e.message;error.hidden=false;throw e;}
  });
  const further=el("details",undefined,controls);el("summary","需要补充调查",further);
  const question=field(further,"补充调查的问题","","textarea");
  button(further,"提交补充调查",()=>act("research_more",{question:question.value}));
}
function render(){const root=$("#detail");root.replaceChildren();const c=detail.case,d=c.data,panel=el("article",undefined,root);el("span",labels[c.state]||c.state,panel,"badge");el("h2",d.rfq?.customer?.company||d.rfq?.customer?.name||d.recipient||c.inquiry_id,panel);el("p",`询盘编号：${c.inquiry_id} · 客户编号：${c.customer_id} · 版本 ${c.version}`,panel,"muted");if(d.due_diligence_approval){const approval=d.due_diligence_approval,approvedAt=approval.at?new Date(approval.at*1000).toLocaleString():"时间未记录";el("p",`背调审批：已通过 · ${approval.actor||"审批人未记录"} · ${approvedAt}${approval.reason?` · 说明：${approval.reason.trim()}`:""}`,panel,"approval-status approved");}if(d.execution_mode==="demo")el("p",d.demo_fixture?"演示模式：调查证据和邮件均为本地模拟数据，不能作为真实业务依据。":"客户邮件来自真实邮箱；报价回复经人工审批后会真实发送给原发件人。",panel,"next-action");el("p",`下一步：${nextAction(c.state)}`,panel,"next-action");const report=detail.artifacts.find(x=>x.artifact_id===d.report_id);if(report&&c.state!=="waiting_due_diligence_review")reportView(el("article",undefined,root),report.payload);const action=el("article",undefined,root);
if(["waiting_customer_info","waiting_rfq_review"].includes(c.state))rfqEditor(action,d);
else if(c.state==="waiting_due_diligence_review"){dueDiligenceReview(action,report);if(d.gaps?.length)rfqEditor(el("section",undefined,action),d);}
else if(c.state==="waiting_negotiation")negotiation(action,d);
else if(c.state==="waiting_quote_approval"){
  const q=detail.artifacts.find(x=>x.artifact_id===d.quote_id),draft=q?.payload||{},calculation=detail.artifacts.find(x=>x.artifact_id===d.quote_calculation_id)?.payload||{},strategy=(draft.reply_agent?.tool_trace||[]).find(x=>x.tool==="choose_reply_strategy");
  el("h2",draft.follow_up_of?"客户跟进邮件草稿":"询盘回复 Agent 草稿",action);
  el("p",draft.follow_up_of?`跟进序号：第 ${draft.follow_up_sequence||1} 次 · 回复 Agent 已沿用报价和客户来信语言生成草稿。`:`执行计划：${draft.reply_agent?.plan_id||"—"}。报价工具已经完成金额核算，回复 Agent 已按客户来信语言生成邮件。`,action,"muted");
  const amount=el("section",undefined,action,"quote-result");el("h3","报价计算工具结果",amount);
  table(amount,["产品","数量","单价","行金额"],(calculation.items||draft.items||[]).map(item=>[item.description||item.name||item.sku||"—",`${item.quantity??"—"} ${item.unit||""}`.trim(),`${item.unit_price??"—"} ${calculation.currency||draft.currency||""}`.trim(),`${item.line_total??"—"} ${calculation.currency||draft.currency||""}`.trim()]));
  el("p",`小计：${calculation.subtotal??draft.subtotal??"—"} ${calculation.currency||draft.currency||""} · 费用：${Object.values(calculation.fees||draft.fees||{}).join(" + ")||"0"} · 总计：${calculation.total??draft.total??"—"} ${calculation.currency||draft.currency||""}`,amount,"next-action");
  if(strategy)el("p",`回复策略：${strategy.strategy} · ${strategy.reason||"未提供原因"} · 来源：${strategy.source||"未知"}`,action,"next-action");
  const subject=field(action,"邮件主题",draft.subject||"");const body=field(action,"邮件正文",draft.body||"","textarea");body.rows=16;
  const a=el("a","下载报价单",action);a.href=`/api/inquiries/${current}/quotation`;
  el("p",`识别语言：${draft.reply_language==="zh"?"中文":draft.reply_language==="en"?"英语":"未记录"} · 收件人：${draft.recipient||"—"}`,action);
  button(action,"保存新草稿版本",()=>act("edit_reply_draft",{subject:subject.value,body:body.value}));
  button(action,draft.follow_up_of?"确认跟进邮件并发送":"确认邮件并调用发送工具",()=>act("approve_reply"));
  if(!draft.follow_up_of){const revise=el("details",undefined,action);el("summary","修改数量、价格或交易条件并重新生成",revise);negotiation(revise,d);}
}
else if(c.state==="needs_message_review"){el("h2","确认新消息的业务意图",action);const intent=select(action,"意图",[["new_inquiry","报价询盘"],["supplement","补充信息"],["change","变更采购需求"],["negotiate","继续谈判"],["acceptance","客户同意报价（仍需核实）"],["reject","拒绝 / 结束"],["unrelated","与该询盘无关"]],"supplement");button(action,"确认并继续",()=>act("classify_message",{intent:intent.value}));}
else if(c.state==="waiting_customer_feedback")followUpPanel(action,d);
else if(c.state==="waiting_acceptance_review"){el("h2","核实客户同意的报价",action);const q=detail.artifacts.find(x=>x.artifact_id===d.sent_quote_id);el("pre",q?.payload.body||"",action);el("p","请选择明确同意此报价版本的客户消息。确认同意不代表已收款或已成交履约。",action);const ids=[];detail.messages.filter(m=>m.created_at>=d.sent_at).forEach(m=>{const l=el("label",undefined,action),i=el("input",undefined,l);i.type="checkbox";ids.push([i,m.message_id]);l.append(document.createTextNode(`${m.payload.sender}: ${m.payload.text.slice(0,300)}`));});const note=field(action,"核实说明","","textarea");button(action,"确认客户同意并保存完整报价",()=>act("confirm_acceptance",{quote_id:d.sent_quote_id,message_ids:ids.filter(([i])=>i.checked).map(([,id])=>id),note:note.value}));}
else if(c.state==="retry_pending"){el("p",`执行失败：${d.error_code}。请处理配置或数据问题后重试。`,action);button(action,"重试当前步骤",()=>act("retry"));}
else if(c.state==="delivery_unknown"){el("p","邮件发送结果不确定。先核对邮箱服务端记录，再决定是否重新发送。",action);const reason=field(action,"核对记录与原因","","textarea");button(action,"已确认发送成功",()=>act("resolve_delivery",{outcome:"sent",reason:reason.value}));button(action,"已确认未发送，返回审批",()=>act("resolve_delivery",{outcome:"not_sent",reason:reason.value}));}
else el("p",c.state==="accepted"?"客户同意的产品、数量、最终价格、条件和回复证据已保存在业务数据库。":`当前阶段：${labels[c.state]||c.state}。可刷新查看进度。`,action);
if(c.state!=="waiting_due_diligence_review"){const notePanel=el("article",undefined,root);if(!renderDemoFallbackNotes(notePanel))notePanel.remove();}
const sources=el("article",undefined,root);el("h2","客户原始消息",sources);for(const m of detail.messages){const box=el("div",undefined,sources,"source");el("strong",`${m.payload.sender} · ${m.payload.sent_at||m.payload.received_at||new Date(m.created_at*1000).toLocaleString()}`,box);el("pre",m.payload.text,box);el("small",`来源编号：${m.message_id}${m.payload.archive?" · 邮件原件已归档 S3":" · 人工导入"}`,box);}const records=el("details",undefined,root);el("summary","保存的业务记录与操作历史",records);for(const a of detail.artifacts){const v=el("details",undefined,records);el("summary",`${a.kind} · 版本 ${a.version}`,v);el("pre",JSON.stringify(a.payload,null,2),v);}table(records,["时间","操作人","操作"],detail.events.map(e=>[new Date(e.created_at*1000).toLocaleString(),e.actor,e.event_type]));if(!["closed","accepted","sending"].includes(c.state)){const close=el("details",undefined,root);el("summary","结束询盘",close);const reason=field(close,"结束原因");button(close,"关闭此询盘",()=>act("close",{reason:reason.value}));}}
const boardCases=()=>demoOnly?cases.filter(c=>c.data?.demo_fixture===true):cases;
function visibleCases(){
  const query=$("#caseSearch").value.trim().toLocaleLowerCase();
  return boardCases().filter(c=>(stageFilter==="all"||stageOf(c.state)===stageFilter)&&
    `${c.inquiry_id} ${c.customer_id} ${c.data?.recipient||""} ${c.data?.rfq?.customer?.company||""} ${c.data?.rfq?.customer?.name||""}`.toLocaleLowerCase().includes(query));
}
function syncCaseSelection(){
  const count=selectedCaseIds.size,remove=$("#deleteSelectedCases"),clear=$("#clearSelectedCases");
  remove.textContent=`删除选中（${count}）`;
  remove.disabled=busy||count===0;
  clear.disabled=busy||count===0;
  $("#selectVisibleCases").disabled=busy||visibleCases().length===0;
}
function renderBoard(){
  const overview=$("#overview"),filters=$("#caseFilters"),list=$("#cases"),shown=boardCases();
  overview.replaceChildren();filters.replaceChildren();list.replaceChildren();
  for(const stage of stages){
    const count=shown.filter(c=>stageOf(c.state)===stage.id).length;
    const card=el("button",undefined,overview,"overview-card"+(stageFilter===stage.id?" active":""));
    card.type="button";el("strong",String(count),card);el("span",stage.label,card);
    card.onclick=()=>{stageFilter=stage.id;renderBoard();};
  }
  for(const stage of [{id:"all",label:`全部 ${shown.length}`},...stages]){
    const chip=el("button",stage.label,filters,stageFilter===stage.id?"active":"");
    chip.type="button";chip.onclick=()=>{stageFilter=stage.id;renderBoard();};
  }
  const visible=visibleCases();
  for(const c of visible){
    const row=el("div",undefined,list,"case-list-row");
    const checkbox=el("input",undefined,row,"case-select-checkbox");
    checkbox.type="checkbox";checkbox.checked=selectedCaseIds.has(c.inquiry_id);
    checkbox.setAttribute("aria-label",`选择 ${c.data?.rfq?.customer?.company||c.data?.rfq?.customer?.name||c.inquiry_id}`);
    checkbox.onchange=()=>{if(checkbox.checked)selectedCaseIds.add(c.inquiry_id);else selectedCaseIds.delete(c.inquiry_id);syncCaseSelection();};
    const card=el("button",undefined,row,"case-card"+(current===c.inquiry_id?" selected":""));
    card.type="button";
    const top=el("span",undefined,card,"case-card-top");
    el("strong",c.data?.rfq?.customer?.company||c.data?.rfq?.customer?.name||c.data?.recipient||c.customer_id,top);
    el("small",labels[c.state]||c.state,top);
    el("small",`${c.inquiry_id} · ${new Date(c.updated_at*1000).toLocaleString()}`,card);
    if(c.data?.due_diligence_approval)el("span","背调审批：已通过",card,"case-approval");
    if(c.state==="waiting_customer_feedback"&&c.data?.next_follow_up_at){const due=new Date(c.data.next_follow_up_at),overdue=due.getTime()<Date.now();el("span",`${overdue?"跟进逾期":"下次跟进"}：${due.toLocaleString()}`,card,overdue?"case-follow-up overdue":"case-follow-up");}
    el("span",`下一步：${nextAction(c.state)}`,card,"next-action");
    card.onclick=async()=>{current=c.inquiry_id;renderBoard();try{detail=await api(`/api/inquiries/${current}`);render();}catch(e){notice(e.message);}};
  }
  if(!visible.length)el("p",demoOnly?"尚无固定演示询盘。点击上方“创建新的演示询盘”。":shown.length?"当前筛选没有询盘。":"暂无询盘。可从下方导入邮件或客户消息。",list,"muted");
  syncCaseSelection();
}
const transientWorkflowStates=new Set(["extracting","researching","preparing_quote","calculating","composing_reply","send_queued","clarify_queued","sending"]);
function scheduleWorkflowRefresh(){clearTimeout(workflowRefreshTimer);workflowRefreshTimer=null;if(activeModule!=="inquiries"||document.hidden||busy)return;const transient=Boolean(detail&&transientWorkflowStates.has(detail.case?.state));if(!transient&&!demoLiveMailbox)return;workflowRefreshTimer=setTimeout(async()=>{if(busy){scheduleWorkflowRefresh();return;}try{await refresh();}catch(error){notice(`自动刷新失败：${error.message}`);scheduleWorkflowRefresh();}},transient?1800:15000);}
async function refresh(){const data=await api("/api/inquiries");cases=data.items||[];const existing=new Set(cases.map(c=>c.inquiry_id));for(const id of selectedCaseIds)if(!existing.has(id))selectedCaseIds.delete(id);const shown=boardCases();if(current&&!shown.some(c=>c.inquiry_id===current))current=null;if(!current&&shown.length)current=shown[0].inquiry_id;renderBoard();if(current){detail=await api(`/api/inquiries/${current}`);render();}else{$("#detail").innerHTML='<div class="empty-detail"><strong>暂无询盘</strong><p>点击“创建新的演示询盘”，或导入一封客户邮件。</p></div>';detail=null;}scheduleWorkflowRefresh();}
async function loadDemoControls(){try{const config=await api("/api/ui/config");demoFixed=Boolean(config.demo_mode&&config.demo_fixed_data);demoLiveMailbox=Boolean(config.demo_live_mailbox);demoOnly=demoFixed&&!requestedCase&&!demoLiveMailbox;$("#demoControl").hidden=!demoFixed;$("#demoToggle").textContent=demoOnly?"查看全部历史":"仅看固定演示";$("#demoMailStatus").textContent=demoLiveMailbox?"本地样例邮件只作模拟；真实邮箱自动收件已开启，真实来信的回复经人工审批后会通过 QQ 邮箱发送。新邮件通常约 1 分钟内进入列表。":"使用本地样例邮件；真实邮箱自动收件已暂停，回复只保存为本地模拟邮件。";}catch{demoFixed=false;demoOnly=false;demoLiveMailbox=false;}}
$("#demoCreate").onclick=()=>run(async()=>{if(!demoFixed)throw Error("当前不是固定演示模式。");const result=await api("/api/inquiries/demo-case",{method:"POST"});current=result.inquiry_id;demoOnly=true;$("#demoToggle").textContent="查看全部历史";stageFilter="all";$("#caseSearch").value="";await refresh();},"已创建独立演示询盘；请按页面提示继续审批。");
$("#demoToggle").onclick=()=>run(async()=>{demoOnly=!demoOnly;$("#demoToggle").textContent=demoOnly?"查看全部历史":"仅看固定演示";current=null;selectedCaseIds.clear();stageFilter="all";await refresh();},"询盘列表已切换。");
$("#selectVisibleCases").onclick=()=>{for(const c of visibleCases())selectedCaseIds.add(c.inquiry_id);renderBoard();};
$("#clearSelectedCases").onclick=()=>{selectedCaseIds.clear();renderBoard();};
$("#deleteSelectedCases").onclick=()=>{
  const selected=cases.filter(c=>selectedCaseIds.has(c.inquiry_id));
  if(!selected.length)return;
  const names=selected.slice(0,5).map(c=>c.data?.rfq?.customer?.company||c.data?.rfq?.customer?.name||c.inquiry_id).join("、");
  if(!window.confirm(`确定删除选中的 ${selected.length} 条询盘吗？\n${names}${selected.length>5?" 等":""}\n相关背调、报价及工作台消息会删除，邮箱中的原邮件不受影响。此操作无法撤销。`))return;
  run(async()=>{
    const items=selected.map(c=>({inquiry_id:c.inquiry_id,expected_version:c.version}));
    try{await api("/api/inquiries/bulk-delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({items})});}
    catch(error){if(error.message.includes("case_delete_busy"))throw Error("选中的询盘正在发送邮件或执行任务，请稍后再删除。");if(error.message.includes("inquiry_version_conflict")){await refresh();throw Error("询盘状态已更新，列表已刷新，请重新核对选择后再删除。");}throw error;}
    if(selectedCaseIds.has(current)){current=null;history.replaceState(null,"",location.pathname);}
    selectedCaseIds.clear();await refresh();
  },`已删除 ${selected.length} 条选中的询盘。`);
};
$("#caseSearch").addEventListener("input",renderBoard);
document.querySelectorAll("[data-module]").forEach(button=>button.onclick=()=>switchModule(button.dataset.module));
$("#refresh").onclick=()=>run(activeModule==="knowledge"?loadKnowledge:activeModule==="support"?loadSupport:activeModule==="fallback"?renderFallback:activeModule==="system"?loadSystem:refresh);
$("#fallbackCategory").onchange=renderFallback;
$("#fallbackBody").addEventListener("input",saveFallbackDraft);
$("#fallbackReset").onclick=()=>{const category=$("#fallbackCategory").value;delete fallbackDrafts[category];$("#fallbackBody").value=fallbackTemplates[category].body;try{localStorage.setItem(fallbackStorageKey,JSON.stringify(fallbackDrafts));notice("已恢复此项默认模板并保存到当前浏览器。");}catch{notice("已恢复默认模板；浏览器禁止本机保存，请复制内容。");}};
$("#fallbackCopy").onclick=async()=>{const value=$("#fallbackBody").value;if(!value.trim()){notice("备用内容为空，请先填写。");return;}try{await navigator.clipboard.writeText(value);notice("已复制备用内容。");}catch{$("#fallbackBody").select();notice("浏览器未允许自动复制，已选中文字，请按 Ctrl+C。");}};
$("#fallbackAttach").onclick=()=>run(async()=>{const category=$("#fallbackCategory").value,body=$("#fallbackBody").value.trim();if(!body)throw Error("请先填写备用说明。");if(!detail||detail.case.inquiry_id!==current)throw Error("请先在询盘工作流中选择演示询盘。");await act("save_demo_fallback_note",{category,body});},"备用说明已保存到当前演示询盘，并与背调证据分开显示。");
$("#supportReload").onclick=()=>run(loadSupport);
$("#supportStatus").onchange=()=>run(loadSupport);
$("#knowledgeReload").onclick=()=>run(loadKnowledge);
$("#knowledgeImport").onclick=()=>$("#knowledgeFileInput").click();
$("#knowledgeFileInput").onchange=e=>run(async()=>{const[file]=e.target.files;try{await importKnowledge(file);}finally{e.target.value="";}});
$("#knowledgeSearch").addEventListener("input",()=>{clearTimeout(loadKnowledge.timer);loadKnowledge.timer=setTimeout(loadKnowledge,250);});
$("#email-import").onsubmit=e=>{e.preventDefault();run(async()=>{const f=new FormData(e.target),result=await api(`/api/inquiries/import-email?account_id=${encodeURIComponent(f.get("account"))}`,{method:"POST",headers:{"Content-Type":"message/rfc822"},body:f.get("file")});current=result.inquiry_id||current;await refresh();});};
$("#wa-import").onsubmit=e=>{e.preventDefault();run(async()=>{const f=new FormData(e.target);if(f.get("attach")&&!current)throw Error("请先选择要关联的询盘。");const result=await api("/api/inquiries/import-whatsapp",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({inquiry_id:f.get("attach")?current:undefined,messages:[{id:f.get("id"),sender:f.get("sender"),name:f.get("name"),text:f.get("text"),sent_at:new Date(f.get("sent_at")).toISOString()}]})});current=result.inquiry_id;await refresh();});};
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&activeModule==="inquiries")scheduleWorkflowRefresh();});
run(async()=>{await loadDemoControls();await refresh();});

