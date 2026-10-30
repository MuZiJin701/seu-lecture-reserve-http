'use strict';
let metadata = null, records = [], selected = null, editing = null, refreshTimer = null, shownChallenge = '';
const $ = (id) => document.getElementById(id);
const optionNames = ['online','offline','trust_device','prewarm','captcha_prefetch','history_background','poller_align','mail_success'];
const phases = {starting:'正在启动',authentication:'正在登录学校',my_activities:'核对已有预约',activities:'查询讲座',captcha:'识别预约验证码',submitting:'提交预约中',confirming:'核对提交结果',waiting:'等待下一次查询',waiting_for_attendance:'预约已覆盖目标，等待参加',manual_required:'等待人工认证',backoff:'等待后重试',done:'选听目标已完成',stopped:'已停止',failed:'运行异常'};
const reasons = {candidate:'可预约',reservation_not_started:'尚未开放',reservation_ended:'预约已结束',capacity_full:'名额已满',not_reservable:'当前不可预约',already_reserved:'已有预约',category_not_allowed:'未启用此类别',category_not_needed:'暂不需要此类别',title_not_selected:'标题不在指定列表',online_disabled:'未启用线上',offline_disabled:'未启用线下',campus_not_selected:'校区不在选择范围',time_conflict:'时间冲突',cooldown:'验证码冷却中',invalid_activity_time:'活动时间无法读取',notice_test_only:'测试或购票公告',retry_limit:'已达安全重试次数'};
const errors = {account_exists:'这个学校账号已添加。',account_already_running:'该账号已在运行，请勿重复启动。',account_stopping:'账号正在停止，请稍后再试。',account_identity_immutable:'一卡通号不可修改，请另建账号。',account_deleting:'该账号正在清理，请重试删除。',reservation_protected:'仍有待确认预约或时间占位，请先启动账号完成核对。',mail_configuration_invalid:'公共发信配置未配置完整或无效，请检查服务器配置。',mail_disabled:'请先为此账号启用邮件并填写收信邮箱。',challenge_expired:'该认证提示已更新，请使用当前提示重新操作。',invalid_answer:'请输入六位数字验证码。',invalid_request:'填写内容有误，请检查必填项、邮箱和选项。',password_required:'新增账号需要填写学校密码。',stop_timeout:'账号尚未停止，请稍后查看状态。',configuration_or_state_invalid:'配置或本地状态异常，请检查服务器上的私有文件。',restart_exhausted:'账号连续异常退出，已停止自动重启，请检查后手动启动。',authentication_rejected:'学校拒绝了账号或密码，请核对配置。',startup_failed:'启动失败，请核对配置和状态。',account_not_found:'账号不存在，可能已被删除。',service_stopping:'服务正在停止，请稍后重试。',state_in_use:'该账号的状态目录正被另一个预约进程占用，请先停止它再操作。',unknown_configuration_key:'配置文件包含已失效或拼写错误的字段，请按 .env.example 清理。',invalid_data_directory:'数据根目录配置为空或无效，请检查 SEU_DATA_DIR。',manager_already_running:'同一数据根已有管理服务在运行，请先停止它。'};
function message(code){return errors[code] || (code?.startsWith('mail_') ? '邮件发送失败，请检查邮箱配置或网络；预约不受影响。' : '操作未完成，请稍后重试或查看账号状态。');}
function node(tag, className, text){const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;}
function timestamp(value){return value ? new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '暂无';}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,5500);}
async function api(path, method='GET', data){
  const headers=method==='GET'?{}:{'Content-Type':'application/json'};
  const response=await fetch('/api/'+path,{method,headers,cache:'no-store',body:method==='GET'?undefined:JSON.stringify(data??{})});
  const body=await response.json().catch(()=>({detail:'request_failed'}));
  if(!response.ok)throw new Error(message(body.detail));return body;
}
async function refresh(){
  clearTimeout(refreshTimer);if(!metadata)return;
  try{records=await api('accounts');if(!records.some(a=>a.id===selected))selected=records[0]?.id??null;render();$('connection-status').textContent=records.some(a=>a.supervisor_healthy===false)?'管理器异常，请检查服务器后重启服务':'状态已更新 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false});}
  catch(error){$('connection-status').textContent='连接暂时中断，正在重试';}
  if(metadata)refreshTimer=setTimeout(refresh,2000);
}
function current(){return records.find(a=>a.id===selected);}
function phase(account){return account.external_running?'CLI 正在运行':account.stopping?'正在停止':account.deleting?'待完成删除':account.error==='restarting'?'稍后自动重启':account.running?(phases[account.health?.phase]||'运行中'):account.status==='done'?'目标已完成':account.error?'需要关注':'已停止';}
function render(){
  $('total-count').textContent=records.length;$('running-count').textContent=records.filter(a=>a.running).length;$('attention-count').textContent=records.filter(a=>a.challenge||a.error&&a.error!=='restarting'||a.protected).length;
  const focusId=document.activeElement?.dataset.account;
  $('account-grid').replaceChildren();$('empty-state').hidden=records.length>0;
  records.forEach(account=>{
    const card=node('button','glass account-card'+(account.id===selected?' selected':''));card.type='button';card.dataset.account=account.id;card.setAttribute('aria-pressed',String(account.id===selected));
    const top=node('div','card-top');top.append(node('strong','',account.name),node('span','badge '+(account.challenge||account.error?'attention':account.running?'running':''),phase(account)));
    const bottom=node('div','card-bottom'),count=node('span');const completed=account.health?.progress?.completed;count.append(node('strong','',completed?Object.values(completed).reduce((a,b)=>a+b,0):'—'),document.createTextNode('/ 8 已选听'));bottom.append(count,node('span','',account.running?'持续跟进 ↗':'查看详情 ↗'));
    card.append(top,node('p','identity','学校账号 · 尾号 '+account.username.slice(-4)),bottom);card.addEventListener('click',()=>{selected=account.id;render();});$('account-grid').append(card);
  });
  if(focusId)$('account-grid').querySelector(`[data-account="${focusId}"]`)?.focus({preventScroll:true});
  renderDetail();
}
function renderDetail(){
  const account=current(),health=account?.health??{},progress=health.progress;
  $('detail-name').textContent=account?.name??'选择一个账号';$('detail-state').textContent=account?phase(account):'查看状态与选听进度';
  const completed=progress?Object.values(progress.completed).reduce((a,b)=>a+b,0):null;
  $('completed-count').textContent=completed??'—';$('goal-progress').value=completed??0;$('planned-count').textContent='待参加：'+(progress?Object.values(progress.planned).reduce((a,b)=>a+b,0):'未知');
  $('goal-progress').setAttribute('aria-valuetext',completed===null?'进度未知':`已选听 ${completed} 场，目标 8 场`);
  $('category-progress').replaceChildren();
  if(metadata)metadata.categories.slice(0,3).forEach((category,index)=>{const covered=progress&&!progress.missing.includes(category);$('category-progress').append(node('span','category-tag'+(covered?' covered':''),['心理健康','法律','艺术'][index]+(covered?' · 已覆盖':' · 待补齐')));});
  $('last-query').textContent=timestamp(health.last_query_at);$('next-check').textContent=timestamp(health.next_check_at);$('candidate-count').textContent=health.candidate_count??'未知';
  $('mail-state').textContent=!account?.mail_enabled?'未启用':({accepted:'SMTP 已接受',failed:'最近发送失败',sending:'正在发送',ready:'已配置',disabled:'未启用'}[health.mail_status]??'尚未发送');
  $('account-error').textContent=account?.error&&account.error!=='restarting'?message(account.error):health.failure_count?('最近错误：'+(health.last_error||'未知')):'';
  $('protected-note').hidden=!account?.protected;
  $('start-button').disabled=!account||account.supervisor_healthy===false||account.running||account.desired||account.deleting;
  $('stop-button').disabled=!account||account.external_running||!account.running&&!account.desired||account.stopping;
  $('edit-button').disabled=!account||account.external_running||account.stopping||account.deleting;
  $('test-mail-button').disabled=!account?.mail_enabled;
  $('delete-button').disabled=!account||account.external_running||account.stopping;
  const challenge=account?.challenge;$('challenge-panel').hidden=!challenge;
  const challengeKey=(account?.id??'')+':'+(challenge?.id??'');if(shownChallenge!==challengeKey){$('sms-code').value='';shownChallenge=challengeKey;}
  if(challenge){$('challenge-label').textContent=challenge.label;const sms=challenge.kind==='sms';$('send-sms').hidden=!sms;$('sms-code').parentElement.hidden=!sms;$('sms-code').required=sms;$('answer-button').textContent=sms?'验证并继续':'已处理，重新检查';}
  $('event-list').replaceChildren();(account?.events??[]).slice().reverse().forEach(event=>{const li=node('li');li.append(node('time','',timestamp(event.at)),node('span','',({task_started:'预约任务已启动',authentication_succeeded:'学校认证成功',authentication_required:'需要人工认证',reservation_succeeded:'预约已确认成功',reservation_unconfirmed:'预约结果待确认',reservation_unconfirmed_released:'已建立时间保护，恢复扫描',goal_completed:'选听目标已完成',task_failed:'任务异常退出',query_failure:'查询持续失败',query_recovered:'查询已恢复',notification_finished:'告警邮件发送结束'}[event.event]??event.event)));$('event-list').append(li);});$('events-empty').hidden=!!account?.events?.length;
  $('selection-list').replaceChildren();(account?.selection??[]).forEach(item=>{const li=node('li');li.append(node('strong','',item.title||'未提供标题'),node('span','',reasons[item.reason]??item.reason));$('selection-list').append(li);});$('selection-empty').hidden=!!account?.selection?.length;
}
function buildChoices(){
  [['category-options',metadata.categories,'categories'],['campus-options',metadata.campuses,'campuses']].forEach(([id,values,name])=>{$(id).replaceChildren();values.forEach(value=>{const label=node('label'),input=node('input');input.type='checkbox';input.name=name;input.value=value;label.append(input,document.createTextNode(value.replace('人文与科学素养系列讲座_','').replace('人文与科学素养系列讲座-','')));$(id).append(label);});});
}
function edit(account){
  editing=account?.id??null;const form=$('account-form');form.reset();$('form-error').textContent='';$('form-title').textContent=account?'配置学校账号':'新增学校账号';
  const opts=account?.options??{online:true,offline:true,prewarm:true,captcha_prefetch:true,history_background:true,categories:metadata.categories,campuses:[]};
  ['name','username','recipient'].forEach(key=>form.elements[key].value=account?.[key]??'');form.elements.username.readOnly=!!account;form.elements.password.required=!account;form.elements.password.value='';form.elements.titles.value=(opts.titles??[]).join('\n');
  optionNames.forEach(key=>form.elements[key].checked=!!opts[key]);form.elements.mail_enabled.checked=!!account?.mail_enabled;
  form.querySelectorAll('[name=categories]').forEach(el=>el.checked=opts.categories?.includes(el.value));form.querySelectorAll('[name=campuses]').forEach(el=>el.checked=opts.campuses?.includes(el.value));$('account-dialog').showModal();
}
async function action(name){const account=current();if(!account)return;try{await api('accounts/'+account.id+'/'+name,'POST');toast(name==='start'?'账号已启动':name==='stop'?'账号已停止':'测试邮件已被 SMTP 接受，请检查收件箱。');await refresh();}catch(error){toast(error.message);}}
document.querySelectorAll('.add-account').forEach(button=>button.addEventListener('click',()=>edit(null)));
$('settings-button').addEventListener('click',()=>edit(current()));$('edit-button').addEventListener('click',()=>edit(current()));$('close-dialog').addEventListener('click',()=>$('account-dialog').close());
$('account-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('[type=submit]');const options={};optionNames.forEach(key=>options[key]=form.elements[key].checked);options.titles=form.elements.titles.value.split('\n').map(s=>s.trim()).filter(Boolean);options.categories=[...form.querySelectorAll('[name=categories]:checked')].map(el=>el.value);options.campuses=[...form.querySelectorAll('[name=campuses]:checked')].map(el=>el.value);const data={name:form.elements.name.value.trim(),username:form.elements.username.value.trim(),password:form.elements.password.value,recipient:form.elements.recipient.value.trim(),mail_enabled:form.elements.mail_enabled.checked,options};button.disabled=true;try{const result=await api('accounts'+(editing?'/'+editing:''),editing?'PUT':'POST',data);selected=result.id;form.elements.password.value='';$('account-dialog').close();toast('账号配置已保存。');await refresh();}catch(error){$('form-error').textContent=error.message;}finally{button.disabled=false;}});
$('start-button').addEventListener('click',()=>action('start'));$('stop-button').addEventListener('click',()=>action('stop'));$('test-mail-button').addEventListener('click',()=>action('test-mail'));
$('delete-button').addEventListener('click',async()=>{const account=current();if(!account)return;if(account.protected){toast(message('reservation_protected'));return;}if(!confirm('删除“'+account.name+'”的本地凭据、配置和运行记录？学校已有预约不会取消。'))return;try{await api('accounts/'+account.id,'DELETE');toast('账号已删除。');await refresh();}catch(error){toast(error.message);}});
async function answer(value){const account=current();if(!account?.challenge)return;try{await api('accounts/'+account.id+'/answer','POST',{challenge:account.challenge.id,answer:value});$('sms-code').value='';toast(value==='send'?'已请求发送短信，请等待学校回复。':'认证输入已提交。');await refresh();}catch(error){toast(error.message);}}
$('send-sms').addEventListener('click',()=>answer('send'));$('challenge-form').addEventListener('submit',event=>{event.preventDefault();answer(current()?.challenge?.kind==='sms'?$('sms-code').value:'');});
document.querySelectorAll('.sidebar nav a').forEach(link=>link.addEventListener('click',()=>{document.querySelectorAll('.sidebar nav a').forEach(el=>el.classList.remove('active'));link.classList.add('active');}));
async function boot(){
  try{metadata=await api('meta');$('today').textContent=new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'});buildChoices();await refresh();}
  catch(error){$('connection-status').textContent='连接管理服务失败，正在重试';setTimeout(boot,2000);}
}
boot();
