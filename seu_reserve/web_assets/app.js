'use strict';
const $ = id => document.getElementById(id);
let admin = null, metadata = null, records = [], selected = null, creating = false;
let dirty = false, saving = false, draftFor = '', draftRevision = 0, refreshTimer, refreshVersion = 0, shownChallenge = '';
let confirmResolve = null;
function confirmChoice(message){
  if(confirmResolve)return Promise.resolve(false);
  $('confirm-message').textContent=message;$('confirm-dialog').showModal();
  return new Promise(resolve=>{confirmResolve=resolve;});
}
$('confirm-accept').addEventListener('click',()=>$('confirm-dialog').close('yes'));
$('confirm-cancel').addEventListener('click',()=>$('confirm-dialog').close('no'));
$('confirm-dialog').addEventListener('cancel',()=>{$('confirm-dialog').returnValue='no';});
$('confirm-dialog').addEventListener('close',()=>{const resolve=confirmResolve;confirmResolve=null;resolve?.($('confirm-dialog').returnValue==='yes');});
const phases = {starting:'正在启动',authentication:'正在登录学校',my_activities:'核对已有预约',activities:'查询讲座',captcha:'识别预约验证码',submitting:'提交预约中',confirming:'核对提交结果',waiting:'等待下次查询',waiting_for_attendance:'等待参加',manual_required:'等待人工认证',backoff:'稍后重试',done:'选听目标已完成',stopped:'已停止',failed:'运行异常'};
const reasons = {candidate:'可预约',reservation_not_started:'尚未开放',reservation_ended:'预约已结束',capacity_full:'名额已满',not_reservable:'当前不可预约',already_reserved:'已有预约',category_not_allowed:'未启用此类别',category_not_needed:'暂不需要此类别',title_not_selected:'标题不在指定列表',online_disabled:'未启用线上',offline_disabled:'未启用线下',campus_not_selected:'校区不在选择范围',time_conflict:'时间冲突',cooldown:'验证码冷却中',invalid_activity_time:'活动时间无法读取',notice_test_only:'测试或购票公告',retry_limit:'已达安全重试次数'};
const errors = {login_rejected:'管理员账号或密码错误。',login_rate_limited:'登录尝试过于频繁，请稍后再试。',authentication_required:'登录已失效，请重新登录。',csrf_rejected:'登录状态已变化，请刷新页面后重试。',account_exists:'这个账号已添加。',account_not_found:'账号已被删除，请选择其他账号。',account_already_running:'该账号已在运行。',account_stopping:'账号正在停止，请稍后再试。',account_identity_immutable:'一卡通号不可修改。',account_deleting:'账号正在清理，请重试删除。',reservation_protected:'仍有待确认预约或时间占位，请先启动账号完成核对。',configuration_conflict:'配置已被其他管理员修改，请重新加载后保存；你的输入仍保留。',mail_configuration_invalid:'公共发信配置无效，请检查服务器配置，或清空此账号收信邮箱。',mail_disabled:'填写收信邮箱后才能发送测试邮件。',challenge_expired:'认证提示已更新，请使用当前提示。',invalid_answer:'请输入六位数字验证码。',invalid_request:'填写内容有误，请检查必填项、邮箱和类别。',password_required:'新增账号需要学校密码。',stop_timeout:'账号尚未停止，请稍后查看。',configuration_or_state_invalid:'配置或本地状态异常，请检查服务器。',restart_exhausted:'账号连续异常退出，请检查后手动启动。',authentication_rejected:'学校拒绝了账号或密码，请核对配置。',service_stopping:'服务正在停止，请稍后重试。',state_in_use:'账号正由 CLI 运行，请先在原终端停止它。'};
function node(tag, className, text){const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;}
function timestamp(value){return value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'暂无';}
function category(value){return (value||'类别未知').replace('人文与科学素养系列讲座_','').replace('人文与科学素养系列讲座-','');}
function total(values){return values?Object.values(values).reduce((a,b)=>a+b,0):null;}
function current(){return creating?null:records.find(account=>account.id===selected);}
function phase(account){return account.external_running?'CLI 正在运行':account.stopping?'正在停止':account.deleting?'待完成删除':account.error==='restarting'?'稍后自动重启':account.running?(phases[account.health?.phase]||'运行中'):account.status==='done'?'目标已完成':account.error?'需要关注':'已停止';}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,5500);}
function showLogin(message=''){
  if($('confirm-dialog').open)$('confirm-dialog').close('no');
  admin=null;clearTimeout(refreshTimer);refreshVersion++;
  $('dashboard').hidden=true;$('user-tools').hidden=true;$('login-view').hidden=false;
  $('login-error').textContent=message;$('login-form').elements.password.value='';
}
async function api(path,method='GET',data){
  const headers=method==='GET'?{}:{'Content-Type':'application/json','X-CSRF-Token':admin?.csrf??''};
  const response=await fetch('/api/'+path,{method,headers,cache:'no-store',body:method==='GET'?undefined:JSON.stringify(data??{})});
  const body=await response.json().catch(()=>({detail:'request_failed'}));
  if(!response.ok){
    if(response.status===401&&path!=='login')showLogin(errors.authentication_required);
    const error=new Error(errors[body.detail]||(body.detail?.startsWith('mail_')?'邮件发送失败，请检查配置或网络。':'操作未完成，请稍后重试。'));
    error.code=body.detail;throw error;
  }
  return body;
}
function buildChoices(){
  [['category-options',metadata.categories,'categories'],['campus-options',metadata.campuses,'campuses']].forEach(([id,values,name])=>{
    $(id).replaceChildren();values.forEach(value=>{const label=node('label'),input=node('input');input.type='checkbox';input.name=name;input.value=value;label.append(input,document.createTextNode(category(value)));$(id).append(label);});
  });
}
function populateForm(account){
  const form=$('account-form');form.reset();dirty=false;draftFor=account?.id??'new';draftRevision=account?.revision??0;
  ['name','username','recipient'].forEach(key=>form.elements[key].value=account?.[key]??'');
  form.elements.username.readOnly=!!account;form.elements.password.required=!account;form.elements.password.value='';
  const options=account?.options??{online:true,offline:true,categories:metadata.categories,campuses:[]};
  ['online','offline'].forEach(key=>form.elements[key].checked=!!options[key]);
  form.querySelectorAll('[name=categories]').forEach(el=>el.checked=options.categories.includes(el.value));
  form.querySelectorAll('[name=campuses]').forEach(el=>el.checked=options.campuses.includes(el.value));
  $('form-error').textContent='';$('reload-config').hidden=true;
}
async function selectAccount(identifier){
  if(saving){toast('正在保存，请稍候。');return;}
  if(identifier===selected&&!creating)return;
  if(dirty&&!await confirmChoice('此账号有未保存的修改，放弃这些修改并切换？'))return;
  selected=identifier;creating=identifier===null;draftFor='';dirty=false;render();
}
function render(){
  const focus=document.activeElement?.dataset.account;
  $('total-count').textContent=records.length;$('running-count').textContent=records.filter(a=>a.running).length;
  $('attention-count').textContent=records.filter(a=>a.challenge||(a.error&&a.error!=='restarting')||a.protected).length;
  $('account-grid').replaceChildren();
  records.forEach(account=>{
    const card=node('button','glass account-card'+(!creating&&account.id===selected?' selected':''));card.type='button';card.dataset.account=account.id;card.setAttribute('aria-pressed',String(!creating&&account.id===selected));
    const top=node('div','card-top');top.append(node('span','account-avatar',account.name.slice(0,1)),node('span','badge '+(account.challenge||account.error?'attention':account.running?'running':''),phase(account)));
    const bottom=node('div','card-bottom'),count=node('span');count.append(node('strong','',total(account.health?.progress?.completed)??'—'),document.createTextNode('/ 8 已选听'));bottom.append(count,node('span','card-arrow','↗'));
    card.append(top,node('p','card-name',account.name),node('p','identity','账号 · 尾号 '+account.username.slice(-4)),bottom);card.addEventListener('click',()=>selectAccount(account.id));$('account-grid').append(card);
  });
  const add=node('button','glass account-card add-card'+(creating?' selected':''));add.type='button';add.dataset.account='new';add.setAttribute('aria-label','新增账号');add.append(node('span','add-symbol','＋'),node('strong','','新增账号'),node('small','','配置后由你启动预约'));add.addEventListener('click',()=>selectAccount(null));$('account-grid').append(add);
  if(focus)$('account-grid').querySelector(`[data-account="${focus}"]`)?.focus({preventScroll:true});
  renderDetail();
}
function renderDetail(){
  const account=current(),health=account?.health??{},progress=health.progress;
  $('detail-name').textContent=account?.name??(dirty&&!creating?'账号已删除':'新增账号');$('detail-state').textContent=account?phase(account):'等待配置';
  $('runtime-detail').hidden=!account;$('reservation-panel').hidden=!account;$('selection-panel').hidden=!account;
  const context=account?.id??(dirty?draftFor:'new');
  if(draftFor!==context||(!dirty&&draftRevision!==(account?.revision??0)))populateForm(account);
  const disabled=saving||!!account&&(account.external_running||account.stopping||account.deleting);
  for(const el of $('account-form').elements)el.disabled=disabled;
  $('draft-state').textContent=dirty?'有未保存的修改':account?'保存后安全生效':'保存后手动启动';
  $('completed-count').textContent=total(progress?.completed)??'—';$('goal-progress').value=total(progress?.completed)??0;
  $('planned-count').textContent='待参加：'+(total(progress?.planned)??'未知');
  $('goal-progress').setAttribute('aria-valuetext',progress?`已选听 ${total(progress.completed)} 场，目标 8 场`:'进度未知');
  $('category-progress').replaceChildren();metadata.categories.slice(0,3).forEach(value=>{$('category-progress').append(node('span','category-tag'+(progress&&!progress.missing.includes(value)?' covered':''),category(value)+(progress&&!progress.missing.includes(value)?' · 已覆盖':' · 待补齐')));});
  $('last-query').textContent=timestamp(health.last_query_at);$('next-check').textContent=timestamp(health.next_check_at);$('candidate-count').textContent=health.candidate_count??'未知';
  $('mail-state').textContent=!account?.mail_enabled?'未启用':({accepted:'SMTP 已接受',failed:'最近发送失败',sending:'正在发送',ready:'已配置',disabled:'未启用'}[health.mail_status]??'尚未发送');
  $('account-error').textContent=account?.error&&account.error!=='restarting'?(errors[account.error]||'账号运行异常，请检查服务器。'):health.failure_count?'最近错误：'+health.last_error:'';
  $('protected-note').hidden=!account?.protected;
  $('start-button').disabled=!account||account.supervisor_healthy===false||account.running||account.desired||account.deleting;
  $('stop-button').disabled=!account||account.external_running||(!account.running&&!account.desired)||account.stopping;
  $('test-mail-button').disabled=!account?.mail_enabled;$('delete-button').disabled=!account||account.external_running||account.stopping;
  const challenge=account?.challenge,key=(account?.id??'')+':'+(challenge?.id??'');$('challenge-panel').hidden=!challenge;
  if(shownChallenge!==key){$('sms-code').value='';shownChallenge=key;}
  if(challenge){const sms=challenge.kind==='sms';$('challenge-label').textContent=challenge.label;$('send-sms').hidden=!sms;$('sms-label').hidden=!sms;$('sms-code').required=sms;$('answer-button').textContent=sms?'验证并继续':'已处理，重新检查';}
  $('event-list').replaceChildren();(account?.events??[]).slice().reverse().forEach(event=>{const li=node('li');li.append(node('time','',timestamp(event.at)),node('span','',({task_started:'预约任务已启动',authentication_succeeded:'学校认证成功',authentication_required:'需要人工认证',reservation_succeeded:'预约已确认成功',reservation_unconfirmed:'预约结果待确认',reservation_unconfirmed_released:'已建立时间保护，继续扫描',goal_completed:'选听目标已完成',task_failed:'任务异常退出',query_failure:'查询持续失败',query_recovered:'查询已恢复',notification_finished:'告警邮件发送结束'}[event.event]??event.event)));$('event-list').append(li);});$('events-empty').hidden=!!account?.events?.length;
  $('selection-list').replaceChildren();(account?.selection??[]).forEach(item=>{const li=node('li');li.append(node('strong','',item.title||'未提供标题'),node('span','',reasons[item.reason]??item.reason));$('selection-list').append(li);});$('selection-empty').hidden=!!account?.selection?.length;
  renderReservations(account);
}
function renderReservations(account){
  const snapshot=account?.reservations,items=snapshot?.items??[];
  $('reservation-count').textContent=snapshot?items.length:'—';
  $('reservation-sync').textContent=snapshot?`${snapshot.stale?'上次有效数据 · ':''}同步于 ${timestamp(snapshot.at)}${!account.running?' · 账号已停止':''}`:'尚未同步';
  $('reservation-list').replaceChildren();items.forEach(item=>{const li=node('li');li.append(node('p','lecture-title',item.title||'活动 '+item.wid),node('p','lecture-meta',`${timestamp(item.start)} — ${timestamp(item.end)}\n${item.location||'地点未提供'} · ${item.online===true?'线上':item.online===false?'线下':'方式未提供'}`),node('span','lecture-tag',category(item.category)+' · 已预约'));$('reservation-list').append(li);});
  $('reservation-empty').hidden=items.length>0;$('reservation-empty').textContent=snapshot?'最近有效数据中没有尚未结束的预约。':'启动账号后同步学校的有效预约。';
  const protections=account?.protections??[];$('protection-panel').hidden=!protections.length;$('protection-list').replaceChildren();
  protections.forEach(item=>{const li=node('li');li.append(node('p','lecture-title',({pending:'提交结果待确认',hold:'未确认的时间占位',confirmed_hold:'已确认，等待学校记录同步'}[item.kind]??'时间保护')),node('p','lecture-meta',`活动 ID：${item.wid}\n${timestamp(item.start)} — ${timestamp(item.end)}`));$('protection-list').append(li);});
}
async function refresh(){
  clearTimeout(refreshTimer);if(!admin)return;const version=++refreshVersion;
  try{const data=await api('accounts');if(version!==refreshVersion||!admin)return;records=data;
    if(!creating&&!dirty&&!records.some(a=>a.id===selected)){selected=records[0]?.id??null;creating=!records.length;}
    render();$('connection-status').textContent=records.some(a=>a.supervisor_healthy===false)?'管理器异常，请检查服务器后重启服务':'状态更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  }catch(error){$('connection-status').textContent='连接暂时中断，正在重试';}
  if(admin&&version===refreshVersion)refreshTimer=setTimeout(refresh,2000);
}
async function enter(session){
  admin=session;$('admin-name').textContent=session.username;const firstLoad=metadata===null;metadata=await api('meta');if(firstLoad)buildChoices();if(!dirty)draftFor='';
  $('login-view').hidden=true;$('dashboard').hidden=false;$('user-tools').hidden=false;await refresh();window.scrollTo({top:0});
}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');button.disabled=true;$('login-error').textContent='';
  try{const session=await api('login','POST',{username:form.elements.username.value.trim(),password:form.elements.password.value});form.elements.password.value='';await enter(session);}
  catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}
});
$('logout-button').addEventListener('click',async()=>{
  if(dirty&&!await confirmChoice('有未保存的修改，放弃并退出登录？'))return;
  try{await api('logout','POST');dirty=false;draftFor='';$('account-form').reset();records=[];selected=null;creating=false;showLogin();}catch(error){toast(error.message);}
});
$('account-form').addEventListener('input',()=>{dirty=true;$('draft-state').textContent='有未保存的修改';});
$('account-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,button=$('save-button'),identifier=creating?null:selected;
  const options={online:form.elements.online.checked,offline:form.elements.offline.checked,categories:[...form.querySelectorAll('[name=categories]:checked')].map(el=>el.value),campuses:[...form.querySelectorAll('[name=campuses]:checked')].map(el=>el.value)};
  const data={name:form.elements.name.value.trim(),username:form.elements.username.value.trim(),password:form.elements.password.value,recipient:form.elements.recipient.value.trim(),options,revision:draftRevision};
  saving=true;renderDetail();button.disabled=true;
  try{const result=await api('accounts'+(identifier?'/'+identifier:''),identifier?'PUT':'POST',data);selected=result.id;creating=false;dirty=false;draftFor='';form.elements.password.value='';toast('账号配置已保存。');await refresh();}
  catch(error){$('form-error').textContent=error.message;$('reload-config').hidden=error.code!=='configuration_conflict';}
  finally{saving=false;renderDetail();}
});
$('reload-config').addEventListener('click',async()=>{if(!dirty||await confirmChoice('放弃当前输入，加载最新保存的配置？')){populateForm(current());renderDetail();}});
async function action(name){const account=current();if(!account)return;try{await api(`accounts/${account.id}/${name}`,'POST');toast(name==='start'?'账号已启动':name==='stop'?'账号已停止':'测试邮件已被 SMTP 接受，请检查收件箱。');await refresh();}catch(error){toast(error.message);}}
$('start-button').addEventListener('click',()=>action('start'));$('stop-button').addEventListener('click',()=>action('stop'));$('test-mail-button').addEventListener('click',()=>action('test-mail'));
$('delete-button').addEventListener('click',async()=>{const account=current();if(!account)return;if(account.protected){toast(errors.reservation_protected);return;}if(!await confirmChoice(`删除“${account.name}”的本地凭据、配置和记录？学校已有预约不会取消。${dirty?' 此账号还有未保存的修改。':''}`))return;try{await api('accounts/'+account.id,'DELETE');dirty=false;draftFor='';toast('账号已删除。');await refresh();}catch(error){toast(error.message);}});
async function answer(value){const account=current();if(!account?.challenge)return;try{await api(`accounts/${account.id}/answer`,'POST',{challenge:account.challenge.id,answer:value});$('sms-code').value='';toast(value==='send'?'已请求发送短信。':'认证输入已提交。');await refresh();}catch(error){toast(error.message);}}
$('send-sms').addEventListener('click',()=>answer('send'));$('challenge-form').addEventListener('submit',event=>{event.preventDefault();answer(current()?.challenge?.kind==='sms'?$('sms-code').value:'');});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
(async()=>{try{await enter(await api('session'));}catch(error){showLogin(error.code==='authentication_required'?'':error.message);}})();
