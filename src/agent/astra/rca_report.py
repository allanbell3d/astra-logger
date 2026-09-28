"""Standard self-contained RCA reports; no model calls or service orchestration."""
from __future__ import annotations
import copy,hashlib,html,json,os,re,tempfile
from datetime import datetime,timezone
from pathlib import Path
VERSION='1.0.0'
STATUSES={'open','repair assigned','repairing','fixed','verified by owner','deferred',"won't fix"}

def timestamp():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def fingerprint(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def embedded(value):return canonical(value).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
def esc(value):return html.escape(str(value))

def new_report(title,report_id,agent,host):
 now=timestamp()
 return {'schema_version':1,'template_version':VERSION,'id':report_id,'title':title,'agent':agent,'affected_hosts':[host],'component':'Unknown','severity':'unknown','status':'open','published_at':now,'updated_at':now,'report_revision':1,'topic':'Incident analysis','project':None,'next_action':'Review the findings; no repair is implied.','rca':{'problem':'Not yet recorded','impact':'Not yet established','cause':'Not yet established','confidence':'unknown','sections':[]},'repair':{'assigned_agent':None,'assigned_host':None,'assigned_at':None,'attempts':[],'followups':[]},'questions':[],'form_version':1,'history':[{'revision':1,'at':now,'actor':agent,'status':'open','note':'Initial RCA report'}]}

def validate(d):
 if d.get('schema_version')!=1 or d.get('template_version')!=VERSION:raise ValueError('Unsupported report/template version')
 for k in ('id','title','agent','severity','next_action'):
  if not isinstance(d.get(k),str) or not d[k].strip() or len(d[k])>2000:raise ValueError('Invalid '+k)
 if len(d['id'])>200:raise ValueError('Report ID too long')
 if d.get('status') not in STATUSES:raise ValueError('Invalid RCA resolution status')
 if type(d.get('report_revision')) is not int or type(d.get('form_version')) is not int:raise ValueError('Integer revisions required')
 if not isinstance(d.get('rca'),dict) or not isinstance(d.get('repair'),dict):raise ValueError('RCA and repair blocks required')
 if d['status'] in ('fixed','verified by owner'):
  attempts=d['repair'].get('attempts',[])
  if not attempts or not attempts[-1].get('verification') or any(v.get('passed') is not True for v in attempts[-1]['verification']):raise ValueError('A fixed report requires recorded passing repair verification')
  if not all(attempts[-1].get(k) for k in ('agent','host','started_at','finished_at','actions')):raise ValueError('Completed repair identity, times and actions required')
 if d['status']=='verified by owner' and not d.get('owner_confirmation'):raise ValueError('Owner confirmation reference required')
 if not isinstance(d.get('questions'),list) or len(d['questions'])>50:raise ValueError('At most 50 questions')
 ids=set()
 for q in d['questions']:
  if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',q.get('id','')) or q['id'] in ids:raise ValueError('Unique safe question IDs required')
  ids.add(q['id'])
  if q.get('type') not in ('choice','multichoice','text','longtext','number','boolean','file'):raise ValueError('Unsupported question type')
  if not isinstance(q.get('label'),str):raise ValueError('Question label required')
  if q['type'] in ('choice','multichoice') and (not isinstance(q.get('choices'),list) or not q['choices'] or any(not isinstance(x,str) for x in q['choices'])):raise ValueError('String choices required')
 if len(canonical(d).encode())>2*1024*1024:raise ValueError('Report exceeds 2 MiB')

def question_html(q):
 attrs=f'name="{esc(q["id"])}" data-kind="{esc(q["type"])}"'+(' required' if q.get('required') else '')
 kind=q['type']
 if kind in ('choice','multichoice','boolean'):
  options=q.get('choices',[]) if kind!='boolean' else ['Yes','No']
  control='<select '+attrs+(' multiple' if kind=='multichoice' else '')+'>'
  if kind!='multichoice':control+='<option value="">Choose…</option>'
  control+=''.join('<option>'+esc(v)+'</option>' for v in options)+'</select>'
 elif kind=='longtext':control='<textarea '+attrs+' maxlength="10000"></textarea>'
 elif kind=='file':control='<input type="file" '+attrs+' multiple>'
 elif kind=='number':control='<input type="number" step="any" '+attrs+''.join(' '+k+'="'+esc(q[k])+'"' for k in ('min','max') if k in q)+'>'
 else:control='<input type="text" '+attrs+' maxlength="'+str(min(q.get('max_length',10000),10000))+'">'
 return '<label>'+esc(q['label'])+(' *' if q.get('required') else '')+control+'</label>'

def render(d):
 validate(d)
 metadata={k:d[k] for k in ('id','title','agent','topic','project','severity','status','published_at','updated_at','report_revision')}
 metadata.update({'date':d['published_at'],'type':d.get('document_type','rca'),'reply-requested':bool(d['questions']) and not d.get('is_template',False),'form_id':'rca-'+d['id'] if d['questions'] and not d.get('is_template',False) else None})
 blocks=[]
 for section in d['rca'].get('sections',[]):blocks.append('<details class="evidence"><summary>'+esc(section['heading'])+'</summary><pre>'+esc(section['text'])+'</pre></details>')
 repair=d['repair'];attempts=[]
 for n,a in enumerate(repair.get('attempts',[]),1):
  attempts.append('<article class="attempt"><h3>Repair attempt '+str(n)+' · '+esc(a.get('agent','Unknown'))+'</h3><p>'+esc(a.get('host',''))+' · '+esc(a.get('started_at',''))+' → '+esc(a.get('finished_at',''))+'</p><h4>Actions performed</h4><ul>'+''.join('<li>'+esc(v)+'</li>' for v in a.get('actions',[]))+'</ul><h4>Changes and backups</h4><ul>'+''.join('<li>'+esc(v)+'</li>' for v in a.get('changes',[]))+'</ul><h4>Verification</h4><ul>'+''.join('<li><b>'+('PASS' if v.get('passed') is True else 'NOT PASSED')+'</b> '+esc(v.get('check',''))+' — '+esc(v.get('result',''))+' <small>'+esc(v.get('evidence_ref',''))+'</small></li>' for v in a.get('verification',[]))+'</ul></article>')
 repair_html=(''.join(attempts) or '<p class="pending">Repair pending — no completed repair recorded.</p>')
 if repair.get('assigned_agent'):repair_html='<p><b>Assigned to '+esc(repair['assigned_agent'])+'</b> · '+esc(repair.get('assigned_host') or 'host not recorded')+' · '+esc(repair.get('assigned_at') or '')+'</p>'+repair_html
 repair_html+='<h3>Open follow-ups</h3>'+('<ul>'+''.join('<li>'+esc(v)+'</li>' for v in repair.get('followups',[]))+'</ul>' if repair.get('followups') else '<p>None recorded.</p>')
 history='<ol>'+''.join('<li><b>Revision '+str(v['revision'])+' · '+esc(v['status'])+'</b> — '+esc(v['at'])+' · '+esc(v['actor'])+'<br>'+esc(v['note'])+'</li>' for v in d['history'])+'</ol>'
 shell=Path(__file__).with_name('rca_report_shell.html').read_text()
 values={'TITLE':esc(d['title']),'VERSION':VERSION,'METADATA':embedded(metadata),'DATA':embedded(d),'STATUS':esc(d['status']),'SEVERITY':esc(d['severity']),'IMPACT':esc(d['rca']['impact']),'HOSTS':esc(', '.join(d['affected_hosts'])),'COMPONENT':esc(d.get('component','Unknown')),'UPDATED':esc(d['updated_at']),'PUBLISHED':esc(d['published_at']),'NEXT':esc(d['next_action']),'AUTHOR':esc(d['agent']),'ID':esc(d['id']),'PROBLEM':esc(d['rca']['problem']),'CAUSE':esc(d['rca']['cause']),'CONFIDENCE':esc(d['rca']['confidence']),'SECTIONS':''.join(blocks),'REPAIR':repair_html,'QUESTIONS':''.join(question_html(q) for q in d['questions']) or '<p>No incident-specific questions. You can still send a comment through the dashboard.</p>','HISTORY':history,'REVISION':str(d['report_revision']),'RCA_HASH':fingerprint(d['rca'])}
 return re.sub(r'@@([A-Z_]+)@@',lambda match:values[match[1]],shell)


def atomic_text(path,text):
 path=Path(path).expanduser();path.parent.mkdir(parents=True,exist_ok=True)
 fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text);f.flush();os.fsync(f.fileno())
  os.chmod(name,0o644);os.replace(name,path)
  fd=os.open(path.parent,os.O_RDONLY)
  try:os.fsync(fd)
  finally:os.close(fd)
 finally:
  if os.path.exists(name):os.unlink(name)


def registration(path,d,shared_root):
 root=Path(shared_root).expanduser().resolve();path=Path(path).expanduser().resolve()
 if not path.is_relative_to(root):raise ValueError('Report outside shared root')
 rel=path.relative_to(root).as_posix()
 if '/' not in rel:raise ValueError('Reports with questions require an agent folder')
 agent=rel.split('/')[0]
 name='rca-'+hashlib.sha256((d['id']+'\0'+rel).encode()).hexdigest()[:24]+'.json'
 target=root/'dashboard/_private/forms'/name
 return target,{'id':'rca-'+d['id'],'version':d['form_version'],'item_path':rel,'agent':agent,'questions':d['questions']}


def publish(path,d,shared_root=None):
 path=Path(path).expanduser();page=render(d)
 target=None;old=None
 if shared_root is not None:
  target,form=registration(path,d,shared_root)
  if target.exists():old=target.read_text()
  if d['questions']:atomic_text(target,canonical(form))
 try:atomic_text(path,page)
 except BaseException:
  if target is not None:
   if old is not None:atomic_text(target,old)
   elif target.exists():target.unlink()
  raise
 if target is not None and not d['questions'] and target.exists():target.unlink()
 return d


def read_report(path):
 raw=Path(path).expanduser().read_text();match=re.search(r'<script type="application/json" id="rca-report-data">(.*?)</script>',raw,re.S)
 if not match:raise ValueError('Not a standardized RCA report')
 data=json.loads(match[1]);validate(data);return data


def update_report(path,change,expected_revision,actor,shared_root=None):
 import fcntl
 path=Path(path).expanduser()
 allowed={'status','repair','questions','next_action','owner_confirmation','reason'}
 if not isinstance(change,dict) or set(change)-allowed:raise ValueError('Repair updates cannot overwrite original RCA or identity')
 fd=os.open(path.parent,os.O_RDONLY)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX)
  before=read_report(path)
  if before['report_revision']!=expected_revision:raise ValueError('Stale report revision; reread before editing')
  d=copy.deepcopy(before);original=fingerprint(d['rca']);status=change.get('status',d['status'])
  transitions={'open':{'repair assigned','deferred',"won't fix"},'repair assigned':{'repairing','deferred',"won't fix"},'repairing':{'fixed','open','deferred',"won't fix"},'fixed':{'verified by owner','open'},'verified by owner':{'open'},'deferred':{'open','repair assigned'},"won't fix":{'open'}}
  if status!=d['status'] and status not in transitions[d['status']]:raise ValueError('Invalid status transition')
  for k in ('status','repair','questions','next_action','owner_confirmation'):
   if k in change:d[k]=copy.deepcopy(change[k])
  previous=before['repair'].get('attempts',[]);current=d['repair'].get('attempts',[])
  if current[:len(previous)]!=previous:raise ValueError('Repair attempt history is append-only')
  if status in ('repair assigned','repairing') and not d['repair'].get('assigned_agent'):raise ValueError('Assigned repairer required')
  if status=='fixed':
   if not current or not current[-1].get('verification') or any(v.get('passed') is not True for v in current[-1]['verification']):raise ValueError('Fixed requires recorded passing verification')
   if not all(current[-1].get(k) for k in ('agent','host','started_at','finished_at','actions')):raise ValueError('Repair actor, host, times and actions required')
  if status=='verified by owner' and not d.get('owner_confirmation'):raise ValueError('Explicit owner confirmation reference required')
  if status in ('deferred',"won't fix") and not change.get('reason'):raise ValueError('Disposition reason required')
  if d['questions']!=before['questions']:d['form_version']+=1
  d['report_revision']+=1;d['updated_at']=timestamp()
  d['history'].append({'revision':d['report_revision'],'at':d['updated_at'],'actor':actor,'status':d['status'],'note':change.get('reason') or 'Repair/status/question update'})
  if fingerprint(d['rca'])!=original:raise ValueError('Original RCA changed')
  publish(path,d,shared_root);return d
 finally:os.close(fd)


def from_markdown(text,finding_id,batch_id,lane,context=None):
 context=context or {};host=str(context.get('host') or 'unknown')
 title=next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('#')),f'Incident {finding_id}')
 d=new_report(title,'rca-'+hashlib.sha256((host+'|'+lane+'|'+batch_id).encode()).hexdigest()[:32],str(context.get('author') or 'astra'),host)
 d['severity']=str(context.get('severity') or 'unknown');d['component']=str(context.get('component') or 'Unknown')
 sections=[];heading='Investigation context';lines=[]
 for line in text.splitlines():
  match=re.match(r'^##\s+(.+)$',line)
  if match:
   if lines:sections.append({'heading':heading,'text':'\n'.join(lines).strip()})
   heading=match[1];lines=[]
  else:lines.append(line)
 if lines:sections.append({'heading':heading,'text':'\n'.join(lines).strip()})
 def excerpt(pattern):
  for section in sections:
   if re.search(pattern,section['heading'],re.I):
    paragraphs=[p.strip() for p in section['text'].split('\n\n') if p.strip()]
    if paragraphs:return paragraphs[0][:420]+('… (full evidence below)' if len(paragraphs[0])>420 else '')
  return 'Not separately stated; see original investigation below.'
 d['rca'].update(problem=title,impact=excerpt('observed.*impact|^impact'),cause=excerpt('root cause'),confidence='See stated confidence in original investigation',sections=sections,source_markdown=text,finding_id=finding_id,batch_id=batch_id,lane=lane)
 block=re.search(r'^```rca-meta\s*\n(.*?)\n```',text,re.M|re.S)
 if block:
  meta=json.loads(block[1])
  if not isinstance(meta,dict):raise ValueError('rca-meta must be an object')
  for key in ('title','severity','component','next_action','topic','project','agent'):
   if key in meta:d[key]=meta[key]
  for key in ('problem','impact','cause','confidence'):
   if key in meta:d['rca'][key]=meta[key]
  if 'questions' in meta:d['questions']=meta['questions']
 validate(d);return d


def main():
 import argparse
 parser=argparse.ArgumentParser(description='Render or update standardized RCA files; never executes repairs')
 sub=parser.add_subparsers(dest='command',required=True)
 render_cmd=sub.add_parser('render');render_cmd.add_argument('data');render_cmd.add_argument('output');render_cmd.add_argument('--shared-root')
 extract=sub.add_parser('extract');extract.add_argument('report');extract.add_argument('output')
 update=sub.add_parser('update');update.add_argument('report');update.add_argument('change');update.add_argument('--expected-revision',type=int,required=True);update.add_argument('--actor',required=True);update.add_argument('--shared-root')
 args=parser.parse_args()
 if args.command=='render':
  if Path(args.output).expanduser().exists():raise SystemExit('Output exists; use update to preserve RCA identity/history')
  publish(args.output,json.loads(Path(args.data).expanduser().read_text()),args.shared_root)
 elif args.command=='extract':
  if Path(args.output).expanduser().exists():raise SystemExit('Extraction output already exists')
  atomic_text(args.output,json.dumps(read_report(args.report),ensure_ascii=False,indent=2))
 else:update_report(args.report,json.loads(Path(args.change).expanduser().read_text()),args.expected_revision,args.actor,args.shared_root)
 print(args.output if hasattr(args,'output') else args.report)


if __name__=='__main__':main()
