// Synthetic bridge, DOM and drop events; never claims real Finder/native UI coverage.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const html = fs.readFileSync(require('node:path').join(__dirname,'../plugins/reference-native-test/panel.html'),'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve=>setImmediate(resolve));
const settle = async (n=6) => { for(let i=0;i<n;i++) await flush(); };

// ---- Minimal DOM: enough structure for bubbling, focus, keyed rows and menus.
function matches(el, selector) {
  return selector.split(',').some(part=>{
    const s=part.trim();
    if(s.startsWith('.')) return el.classList.contains(s.slice(1));
    if(s.startsWith('#')) return el.id===s.slice(1);
    return el.tagName===s;
  });
}
function makeDocument() {
  const doc = {activeElement:null, handlers:{}};
  class El {
    constructor(tag,id) { Object.assign(this,{tagName:tag,id,children:[],parentNode:null,dataset:{},attrs:{},handlers:{},_text:'',_classes:new Set(),hidden:false,disabled:false,value:''});
      this.style={setProperty(k,v){this[k]=v;},removeProperty(k){delete this[k];}}; }
    get className() { return [...this._classes].join(' '); }
    set className(v) { this._classes=new Set(String(v).split(/\s+/).filter(Boolean)); }
    get classList() { const c=this._classes; return {add:(...n)=>n.forEach(x=>c.add(x)),remove:(...n)=>n.forEach(x=>c.delete(x)),contains:n=>c.has(n),toggle:(n,f)=>{const on=f===undefined?!c.has(n):Boolean(f); on?c.add(n):c.delete(n); return on;}}; }
    get textContent() { return this.children.length ? this.children.map(c=>c.textContent).join('') : this._text; }
    set textContent(v) { this.children.forEach(c=>{c.parentNode=null;}); this.children=[]; this._text=String(v); }
    get firstChild() { return this.children[0] || null; }
    get nextSibling() { const p=this.parentNode; return p ? p.children[p.children.indexOf(this)+1] || null : null; }
    get offsetWidth() { return 0; } get offsetHeight() { return 0; }
    append(...nodes) { for(const n of nodes) { if(typeof n==='string') { const t=new El('#text'); t._text=n; nodes=[t]; this.append(t); continue; } n.remove(); n.parentNode=this; this.children.push(n); } }
    insertBefore(n,ref) { n.remove(); const i=ref?this.children.indexOf(ref):-1; if(i<0) this.children.push(n); else this.children.splice(i,0,n); n.parentNode=this; }
    replaceChildren(...nodes) { this.children.forEach(c=>{c.parentNode=null;}); this.children=[]; this._text=''; this.append(...nodes); }
    remove() { const p=this.parentNode; if(!p) return; p.children.splice(p.children.indexOf(this),1); this.parentNode=null; if(this.contains(doc.activeElement)) doc.activeElement=doc.body; }
    contains(n) { while(n) { if(n===this) return true; n=n.parentNode; } return false; }
    closest(sel) { let n=this; while(n) { if(n.tagName && matches(n,sel)) return n; n=n.parentNode; } return null; }
    querySelectorAll(sel) { const out=[]; const walk=n=>n.children.forEach(c=>{ if(matches(c,sel)) out.push(c); walk(c); }); walk(this); return out; }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    setAttribute(k,v) { this.attrs[k]=String(v); }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    removeAttribute(k) { delete this.attrs[k]; delete this[k]; }
    addEventListener(type,fn) { (this.handlers[type] ||= []).push(fn); }
    dispatch(type,init={}) {
      const e={type,target:this,defaultPrevented:false,stopped:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;},...init};
      const results=[]; let n=this;
      while(n && !e.stopped) { for(const fn of [...(n.handlers[type] || []),...(typeof n['on'+type]==='function'?[n['on'+type]]:[])]) results.push(fn(e)); n=n.parentNode; }
      if(!e.stopped) for(const fn of doc.handlers[type] || []) results.push(fn(e));
      return Promise.all(results);
    }
    click(init) { return this.dispatch('click',{detail:1,...init}); }
    focus() { doc.activeElement=this; }
    getBoundingClientRect() { return {left:20,top:100,right:300,bottom:130,width:280,height:30}; }
    scrollIntoView() {}
    pause() { this.paused=true; } load() {}
  }
  const registry=new Map();
  Object.assign(doc,{El,registry,
    getElementById(id) { if(!registry.has(id)) registry.set(id,new El('div',id)); return registry.get(id); },
    createElement:tag=>new El(tag), createElementNS:(_,tag)=>new El(tag),
    addEventListener(type,fn) { (doc.handlers[type] ||= []).push(fn); },
    dispatch(type,init={}) { const e={type,preventDefault(){},stopPropagation(){},...init}; return Promise.all((doc.handlers[type] || []).map(fn=>fn(e))); },
    execCommand:()=>true});
  doc.documentElement=new El('html'); doc.body=new El('body');
  // Seed the markup's initial hidden/disabled state for elements the script uses.
  for(const [, tag, attrs] of html.matchAll(/<(\w+)\s([^>]*\bid="[^"]+"[^>]*)>/g)) {
    const el=doc.getElementById(attrs.match(/\bid="([^"]+)"/)[1]);
    el.tagName=tag; el.hidden=/\shidden(\s|$)/.test(' '+attrs); el.disabled=/\sdisabled(\s|$)/.test(' '+attrs);
  }
  return doc;
}

// ---- Fake host + server with per-thread revisioned lists.
function boot({failFirstRead=false, list=[], theme={theme:'light'}, thumbnails=false, scope='conversation', serverState=null}={}) {
  const doc=makeDocument();
  const windowHandlers={};
  const server=serverState || {revision:list.length?3:0, references:structuredClone(list), saves:[], conflicts:0,scope,scopeId:'fixture-'+scope,project:null};
  const h={doc,server,captures:[],actions:[],previewRequests:[],releases:[],blobURLs:[],revokedURLs:[],hostReplies:[],deferredChunks:[],deferredPreviews:[],calls:[],thumbRequests:[],
    pickerStatus:'selected',pickerFiles:[{path:'/fixture/中文 视频.mp4',exists:true,isFile:true},{path:'/fixture/文档.txt',exists:true,isFile:true}],
    dragAvailable:false,actionError:null,deferPreview:false,deferChunks:false,previewSize:6,previewChunkSize:4,lastBlob:null,failRead:failFirstRead,holdSave:null};
  class TestURL extends URL {
    static createObjectURL(blob) { assert.ok(blob instanceof Blob); h.lastBlob=blob; const url='blob:fixture/'+h.blobURLs.length; h.blobURLs.push(url); return url; }
    static revokeObjectURL(url) { h.revokedURLs.push(url); }
  }
  const window={addEventListener(type,fn){ (windowHandlers[type] ||= []).push(fn); },innerWidth:400,innerHeight:800,location:{protocol:'https:'}};
  h.emit=(type,e={})=>Promise.all((windowHandlers[type] || []).map(fn=>fn(e)));
  h.deliver=data=>h.emit('message',{source:window.parent,data});
  window.parent={postMessage(message){
    if(!('id' in message)) return;
    if(!('method' in message)) { h.hostReplies.push(message); return; }
    let result;
    if(message.method==='ui/initialize') result={hostInfo:{name:'fixture-host'},hostContext:theme};
    else {
      assert.equal(message.method,'tools/call');
      const {name,arguments:args}=message.params;
      h.calls.push(name);
      const ok=data=>({structuredContent:{probeContext:{requestMeta:{threadId:'fixture-A'},serverCwd:'/fixture/plugin'},...data}});
      const error=(text,code)=>({isError:true,content:[{type:'text',text}],structuredContent:{error:{code,message:text}}});
      if(name==='read_probe_context') {
        if(h.failRead) { h.failRead=false; result=error('fixture read failed','store'); }
        else result=ok({...(args.knownRevision===server.revision && args.scopeId===server.scopeId?{unchanged:true}:{references:structuredClone(server.references)}),listRevision:server.revision,listScope:server.scope,scopeId:server.scopeId,project:server.project,projectLookup:server.projectLookup});
      } else if(name==='save_reference_list') {
        if((server.scope==='project' || args.scopeId!=null) && args.scopeId!==server.scopeId) result=error('项目已变更','scope-changed');
        else if(args.baseRevision!==server.revision) { server.conflicts++; result=ok({list:{status:'conflict',revision:server.revision,references:structuredClone(server.references)}}); }
        else { server.revision++; server.references=structuredClone(args.references); server.saves.push(structuredClone(args.references)); result=ok({list:{status:'saved',revision:server.revision,references:structuredClone(args.references)}}); }
      } else if(name==='save_probe_snapshot') { h.captures.push(structuredClone(args.snapshot)); result=ok({saved:true,eventId:'fixture-event'}); }
      else if(name==='reference_file_action') { h.actions.push(structuredClone(args)); result=h.actionError?error(h.actionError):ok({fileAction:{status:'requested',action:args.action}}); }
      else if(name==='release_reference_preview') { h.releases.push(args.leaseId); result=ok({released:true}); }
      else if(name==='choose_reference_files') result=ok({pickerRecordSaved:true,eventId:'pick',selection:h.pickerStatus==='error'?{status:'error',message:'fixture picker error'}:{status:h.pickerStatus,source:'NSOpenPanel',files:h.pickerStatus==='selected'?structuredClone(h.pickerFiles):[],allPathsExist:true}});
      else if(name==='cancel_reference_selection') result=ok({cancelRequested:true});
      else if(name==='read_reference_thumbnails') {
        h.thumbRequests.push(...args.paths);
        const items=Object.fromEntries(args.paths.map(path=>[path,!server.references.some(f=>f.path===path)?{status:'not-listed'}:
          thumbnails?{status:'ok',mime:'image/jpeg',data:Buffer.from('\xff\xd8thumb').toString('base64'),...(/\.mp4$/.test(path)?{duration:35.2}:{})}:{status:'none'}]));
        result={structuredContent:{thumbnailsReady:true},_meta:{referenceThumbnails:items}};
      }
      else if(name==='capture_reference_drag') result=ok({dragCapture:{id:h.dragAvailable?'fixture-drag':null,available:h.dragAvailable}});
      else if(name==='resolve_reference_drop') {
        const files=args.paths.length?args.paths.map(path=>({path,name:path.split('/').pop(),size:100,exists:true,isFile:true})):h.dragAvailable?args.files.map(f=>({...f,path:'/fixture/'+f.name,exists:true,isFile:true})):[];
        result=ok({selection:files.length?{status:'selected',source:args.paths.length?'browser-drop-path':'NSPasteboardNameDrag.fileURL',files,allPathsExist:true}:{status:'unavailable',message:'未能取得原文件路径，请点「添加」选择这些文件',files:[]}});
      } else if(name==='prepare_reference_preview') {
        h.previewRequests.push(structuredClone(args));
        if(!server.references.some(f=>f.path===args.path)) result=error('请先将文件添加到当前参考列表','not-listed');
        else if(h.actionError) result=error(h.actionError);
        else {
          const kind=/\.html?$/i.test(args.path)?'html':/\.pdf$/i.test(args.path)?'pdf':/\.txt$/i.test(args.path)?'text':'video';
          result={structuredContent:{previewReady:true},_meta:{referencePreview:{kind,transport:'mcp-chunks',byteSize:h.previewSize,chunkSize:h.previewChunkSize,mime:'video/mp4',leaseId:'fixture-'+h.previewRequests.length,page:Math.min(args.page || 1,2),pageCount:2,text:'<script>plain text</script>'}}};
        }
      } else if(name==='read_reference_preview_chunk') {
        const bytes=h.previewSize===6?Buffer.from('abcdef').subarray(args.offset,args.offset+4):Buffer.alloc(Math.min(h.previewChunkSize,h.previewSize-args.offset),Math.floor(args.offset/h.previewChunkSize)%256);
        result={structuredContent:{chunkReady:true},_meta:{referenceChunk:{offset:args.offset,totalBytes:h.previewSize,byteLength:bytes.length,data:bytes.toString('base64')}}};
      } else result=ok({});
    }
    const reply=()=>h.deliver({jsonrpc:'2.0',id:message.id,result});
    const name=message.params?.name;
    if(h.deferPreview && name==='prepare_reference_preview') h.deferredPreviews.push({deliver:reply,result});
    else if(h.deferChunks && name==='read_reference_preview_chunk') h.deferredChunks.push(reply);
    else if(h.holdSave && name==='save_reference_list') h.holdSave.push({id:message.id,reply});
    else queueMicrotask(reply);
  }};
  const timers=(fn,ms)=>{ if(ms===3000) h.pollShelf=fn; const t=setTimeout(fn,ms); t.unref?.(); return t; };
  const context={window,document:doc,crypto:{randomUUID},navigator:{clipboard:{async writeText(text){ h.copied=text; }}},URL:TestURL,Blob,Uint8Array,atob,Date,Map,Set,
    setTimeout:timers,clearTimeout,console,Promise,Error,JSON,Math,Number,Object,Array,String,Boolean,structuredClone,queueMicrotask};
  vm.runInNewContext(script, context);
  h.$=id=>doc.getElementById(id);
  h.rows=()=>h.$('file-list').children;
  h.row=text=>h.rows().find(r=>r.dataset.path.includes(text));
  h.drop=(files,strings={})=>({dataTransfer:{files,items:files.map(f=>({kind:'file',webkitGetAsEntry:()=>({name:f.name,fullPath:'/'+f.name,isFile:true,isDirectory:false})})),
    types:['Files',...Object.keys(strings)],getData:t=>strings[t]||'',dropEffect:''}});
  return h;
}

(async()=>{
  // ---- Restore, theme and diagnostics.
  const existing={path:'/fixture/已有 素材.mp4',name:'已有 素材.mp4',size:100,exists:true,isFile:true};
  const vars={'--color-background-primary':'rgb(253, 246, 227)','--color-text-primary':'rgb(92, 106, 114)','--evil':'url(https://x)','bad name':'red'};
  let h=boot({list:[existing],theme:{theme:'light',styles:{variables:vars}}});
  await settle();
  const root=h.doc.documentElement;
  assert.equal(root.style['--color-background-primary'],'rgb(253, 246, 227)','host theme variables drive the panel');
  assert.equal(root.style['--evil'],undefined,'url() values are not applied');
  assert.equal(root.style['bad name'],undefined);
  assert.equal(h.$('file-count').textContent,'1','saved list is restored');
  assert.equal(h.$('refresh').disabled,false);
  assert.match(h.$('identity').textContent,/fixture-A/);
  assert.equal(h.$('preview-board').hidden,true,'no empty preview box before a file is chosen');
  assert.equal(h.row('已有').children[0].children[0].dataset.state,'none','missing thumbnails fall back to the kind icon');
  assert.ok(h.thumbRequests.includes(existing.path),'thumbnails are requested for listed files');

  // ---- Drops.
  await h.doc.dispatch('drop',h.drop([{name:'<img onerror=fixture>.mp4',size:100,type:'video/mp4',lastModified:1}]));
  await settle();
  assert.equal(h.$('file-count').textContent,'1','missing path must not create a fake reference');
  assert.match(h.$('picker-status').textContent,/未能取得原文件路径/);
  assert.equal(h.$('status-line').classList.contains('error'),true);
  assert.match(h.$('drop-data').textContent,/<img onerror=fixture>/);
  assert.ok(!html.includes('.innerHTML'),'file names must render as text');
  assert.ok(!/readAs|arrayBuffer\(|fetch\(/.test(script),'drop probe must not read/upload file bytes');
  const original='/Users/fixture/参考 material.pdf';
  await h.doc.dispatch('drop',h.drop([{name:'参考 material.pdf',size:100,type:'application/pdf',lastModified:1}],{'text/uri-list':'file://'+encodeURI(original)}));
  await settle();
  assert.equal(h.$('file-count').textContent,'2');
  assert.ok(h.server.references.some(f=>f.path===original),'drop reaches the per-chat list');
  assert.match(h.$('picker-status').textContent,/已添加 1 个参考/);
  assert.equal(h.$('status-line').classList.contains('error'),false,'success clears the error style');
  h.dragAvailable=true;
  const videos=[{name:'视频 A.mp4',size:100,type:'video/mp4',lastModified:1},{name:'视频 B.MOV',size:100,type:'video/quicktime',lastModified:1}];
  await h.doc.dispatch('dragenter',{dataTransfer:h.drop(videos).dataTransfer});
  assert.equal(h.doc.body.classList.contains('dragging'),true);
  const rowsBefore=[...h.rows()];
  await settle();
  assert.deepEqual([...h.rows()],rowsBefore,'the drag-capture result must not rebuild rows under the cursor');
  await h.doc.dispatch('dragleave',{relatedTarget:null});
  assert.equal(h.doc.body.classList.contains('dragging'),false,'leaving the panel clears the overlay');
  await h.doc.dispatch('dragenter',{dataTransfer:h.drop(videos).dataTransfer}); await settle();
  await h.doc.dispatch('drop',h.drop(videos)); await settle();
  assert.equal(h.$('file-count').textContent,'4','native fallback adds both dropped videos');
  await h.doc.dispatch('drop',h.drop(videos)); await settle();
  assert.equal(h.$('file-count').textContent,'4','duplicate drop must not duplicate visible rows');
  assert.match(h.$('picker-status').textContent,/已在列表中/);
  await h.doc.dispatch('dragstart',{}); await h.doc.dispatch('dragenter',{dataTransfer:h.drop(videos).dataTransfer});
  assert.equal(h.doc.body.classList.contains('dragging'),false,'dragging the preview image is not an add');
  await h.doc.dispatch('dragend',{});

  // ---- Picker.
  await h.$('choose').click(); await settle();
  assert.equal(h.$('file-count').textContent,'6');
  assert.match(h.$('picker-status').textContent,/已添加 2 个参考/);
  h.pickerStatus='cancelled'; await h.$('choose').click(); await settle();
  assert.match(h.$('picker-status').textContent,/已取消/);
  h.pickerStatus='error'; await h.$('choose').click(); await settle();
  assert.match(h.$('picker-status').textContent,/fixture picker error/);
  assert.equal(h.$('choose').disabled,false);
  h.pickerStatus='selected';

  // ---- PDF preview and paging.
  const pdfRow=h.row('material.pdf');
  await pdfRow.children[0].click(); await settle();
  assert.equal(h.actions.length,0,'preview must never invoke system-open or Quick Look');
  assert.equal(h.previewRequests.at(-1).path,original);
  assert.equal(h.$('preview-board').hidden,false);
  assert.equal(h.$('preview-content').children[0].tagName,'img','PDF renders inside the board');
  assert.equal(h.$('preview-content').children[0].draggable,false);
  await h.$('preview-content').children[0].dispatch('load'); await settle();
  assert.equal(h.$('page-number').textContent,'1 / 2');
  assert.equal(pdfRow.dataset.current,'true');
  await h.$('next-page').click(); await settle();
  assert.equal(h.previewRequests.at(-1).page,2);
  await h.$('preview-content').children[0].dispatch('load'); await settle();
  assert.equal(h.$('next-page').disabled,true);
  assert.equal(h.revokedURLs.length,h.blobURLs.length-1,'the previous page is revoked once the next is shown');

  // ---- Context menu, keyboard and Finder.
  await pdfRow.dispatch('contextmenu',{clientX:100,clientY:100});
  assert.equal(h.$('file-menu').hidden,false);
  const menuItems=()=>h.$('file-menu').querySelectorAll('button');
  assert.equal(menuItems()[0].textContent,'在面板预览');
  assert.equal(h.doc.activeElement,menuItems()[0],'menu focuses its first item');
  await h.$('file-menu').dispatch('keydown',{key:'ArrowDown'});
  assert.equal(h.doc.activeElement,menuItems()[1]);
  await menuItems()[1].click(); await settle();
  assert.equal(h.actions.at(-1).action,'reveal','Show in Finder must reach the backend');
  await pdfRow.children[1].click({detail:0}); // ⋯ by keyboard
  assert.equal(pdfRow.children[1].getAttribute('aria-expanded'),'true');
  await h.$('file-menu').dispatch('keydown',{key:'Escape'});
  assert.equal(h.$('file-menu').hidden,true);
  assert.equal(h.doc.activeElement,pdfRow.children[1],'Escape returns focus to the trigger');

  // ---- Video preview, focus survival, no restart.
  const videoRow=h.row('视频 A.mp4');
  videoRow.children[0].focus();
  await videoRow.children[0].click(); await settle();
  assert.equal(h.actions.at(-1).action,'reveal','video preview must not dispatch another system action');
  const video=h.$('preview-content').children[0];
  assert.equal(video.tagName,'video'); assert.equal(video.controls,true);
  await video.dispatch('loadeddata'); await settle();
  assert.match(h.$('preview-status').textContent,/视频/);
  const requestsBefore=h.previewRequests.length;
  await videoRow.children[0].click(); await settle();
  assert.equal(h.previewRequests.length,requestsBefore,'clicking the shown file must not restart or re-download it');
  await h.$('refresh').click(); await h.$('save').click(); await settle();
  assert.equal(h.$('preview-content').children[0],video,'context refresh must preserve playback');
  assert.equal(h.row('视频 A.mp4'),videoRow,'rows are updated in place');
  assert.equal(h.doc.activeElement,videoRow.children[0],'tool results must not steal keyboard focus');
  await videoRow.children[0].dispatch('keydown',{key:'ArrowRight'});
  assert.equal(h.doc.activeElement,h.row('视频 B.MOV').children[0],'ArrowRight moves to the next item');
  await h.row('视频 B.MOV').children[0].dispatch('keydown',{key:'ArrowLeft'});
  assert.equal(h.doc.activeElement,videoRow.children[0]);
  assert.equal(h.$('file-list').classList.contains('grid'),true,'the thumbnail grid is the default view');
  await h.$('view-list').click();
  assert.equal(h.$('file-list').classList.contains('list'),true);
  assert.equal(h.$('view-list').getAttribute('aria-pressed'),'true');
  assert.equal(h.row('视频 A.mp4'),videoRow,'switching views keeps the same rows');
  await videoRow.children[0].dispatch('keydown',{key:'ArrowDown'});
  assert.equal(h.doc.activeElement,h.row('视频 B.MOV').children[0],'ArrowDown moves one row in the list view');
  await h.$('view-grid').click();

  // ---- Failure, retry and close.
  await videoRow.dispatch('contextmenu',{clientX:100,clientY:100});
  h.actionError='fixture preview failed';
  await menuItems()[0].click(); await settle();
  assert.equal(video.paused,true,'switching previews must stop old playback');
  assert.match(h.$('preview-message-text').textContent,/fixture preview failed/);
  assert.equal(h.$('retry-preview').hidden,false);
  assert.equal(h.captures.at(-1).events.at(-1).type,'preview-error','failed previews must persist diagnostic evidence');
  h.actionError=null;
  await h.$('retry-preview').click(); await settle();
  const nextVideo=h.$('preview-content').children[0];
  await h.$('close-preview').click(); await settle();
  assert.equal(nextVideo.paused,true);
  assert.equal(h.$('preview-board').hidden,true,'closing hides the board instead of leaving a placeholder');
  assert.ok(h.releases.length>=3,'switch and close must release local media leases');

  // ---- Late responses.
  h.deferPreview=true;
  const oldPreview=pdfRow.children[0].click(); await settle();
  assert.equal(h.deferredPreviews.length,1);
  h.deferPreview=false;
  await videoRow.children[0].click(); await settle();
  const currentVideo=h.$('preview-content').children[0];
  h.deferredPreviews[0].deliver(); await oldPreview; await settle();
  assert.equal(h.$('preview-content').children[0],currentVideo,'late PDF result must not replace the newly selected video');
  assert.ok(h.releases.includes(h.deferredPreviews[0].result._meta.referencePreview.leaseId),'late preview must release its lease');
  await h.row('文档.txt').children[0].click(); await settle();
  assert.equal(currentVideo.paused,true);
  assert.equal(h.$('preview-content').children[0].tagName,'pre');
  assert.equal(h.$('preview-content').children[0].textContent,'<script>plain text</script>');

  // HTML uses an isolated page, keeps the whole chunk transport, and cleans up on close.
  const previousHarness=h;
  const article={path:'/fixture/article.html',name:'article.html',size:3000000,exists:true,isFile:true};
  h=boot({list:[article]}); await settle();
  await h.row('article.html').children[0].click(); await settle();
  const frame=h.$('preview-content').children[0];
  assert.equal(frame.tagName,'iframe');
  assert.equal(frame.getAttribute('sandbox'),'','HTML never receives scripts, same-origin or host access');
  assert.equal(frame.getAttribute('referrerpolicy'),'no-referrer');
  assert.equal(frame.srcdoc,'abcdef');
  await frame.dispatch('load'); await settle();
  assert.match(h.$('preview-status').textContent,/网页/);
  await h.$('close-preview').click(); await settle();
  assert.equal(h.$('preview-content').children.length,0);
  assert.equal(h.blobURLs.length,0,'HTML documents do not navigate to a blob URL');

  // Restore the earlier harness for the remaining media checks.
  h=previousHarness;

  // ---- Large file assembly and cancellation.
  h.previewSize=67323662; h.previewChunkSize=512*1024;
  await h.row('视频 B.MOV').children[0].click(); await settle();
  assert.equal(h.lastBlob.size,h.previewSize,'the actual reported file size must pass the frontend too');
  for(const offset of [0,h.previewChunkSize-2,32*1024*1024,h.previewSize-4]) {
    const bytes=new Uint8Array(await h.lastBlob.slice(offset,offset+4).arrayBuffer());
    for(let i=0;i<bytes.length;i++) assert.equal(bytes[i],Math.floor((offset+i)/h.previewChunkSize)%256,'assembled bytes must stay in order');
  }
  const urlsBeforeCancel=h.blobURLs.length;
  h.deferChunks=true;
  const cancelled=videoRow.children[0].click(); await settle();
  assert.equal(h.deferredChunks.length,3,'only three requests may be in flight');
  await h.$('close-preview').click();
  h.deferredChunks.forEach(deliver=>deliver());
  await cancelled; await settle();
  assert.equal(h.deferredChunks.length,3,'close must stop reading more chunks');
  assert.equal(h.blobURLs.length,urlsBeforeCancel,'cancelled reads must not create a Blob URL');
  assert.ok(h.releases.includes('fixture-'+h.previewRequests.length),'cancelled read releases its lease');
  h.deferChunks=false; h.previewSize=6; h.previewChunkSize=4;

  // ---- Remove, undo, multi-select.
  const count=()=>Number(h.$('file-count').textContent);
  const before=count(), orderBefore=h.rows().map(r=>r.dataset.path);
  await pdfRow.children[1].click({clientX:100,clientY:100});
  await [...menuItems()].find(b=>b.textContent==='移除参考').click(); await settle();
  assert.equal(count(),before-1,'remove deletes the reference row');
  assert.ok(!h.server.references.some(f=>f.path===original),'removal is saved');
  assert.equal(h.$('status-action').textContent,'撤销');
  await h.$('status-action').click(); await settle();
  assert.deepEqual(h.rows().map(r=>r.dataset.path),orderBefore,'undo restores the row at its position');
  assert.ok(h.server.references.some(f=>f.path===original));
  await h.row('视频 A.mp4').children[0].click({metaKey:true});
  await h.row('视频 B.MOV').children[0].click({metaKey:true});
  assert.equal(h.$('bulk-bar').hidden,false);
  assert.equal(h.$('bulk-count').textContent,'已选 2 项');
  assert.equal(h.row('视频 A.mp4').classList.contains('selected'),true);
  h.row('视频 A.mp4').children[0].focus();
  await h.row('视频 A.mp4').children[0].dispatch('keydown',{key:'Delete'}); await settle();
  assert.equal(count(),before-2,'Delete removes the whole selection');
  assert.ok(!h.server.references.some(f=>/视频 [AB]/.test(f.path)));
  assert.equal(h.$('bulk-bar').hidden,true);
  assert.equal(h.doc.activeElement?.closest?.('.file-row') !== null,true,'focus moves to a remaining row');

  // ---- Another panel wrote meanwhile: our edit is replayed onto its list.
  h.server.revision++; h.server.references.push({path:'/fixture/另一面板.mov',name:'另一面板.mov',exists:true});
  await h.row('文档.txt').children[1].click({clientX:1,clientY:1});
  await [...menuItems()].find(b=>b.textContent==='移除参考').click(); await settle();
  assert.equal(h.server.conflicts>=1,true);
  assert.ok(h.server.references.some(f=>f.path==='/fixture/另一面板.mov'),'the other panel\'s add survives');
  assert.ok(!h.server.references.some(f=>f.path==='/fixture/文档.txt'),'our removal is still applied');
  assert.ok(h.row('另一面板'),'the merged list is shown');

  // ---- Host requests never resolve our pending calls.
  h.holdSave=[];
  await h.row('中文 视频').children[1].click({clientX:1,clientY:1});
  const removal=[...menuItems()].find(b=>b.textContent==='移除参考').click(); await settle();
  assert.equal(h.holdSave.length,1);
  const pendingId=h.holdSave[0].id;
  // The host numbers its own requests; a ping reusing our pending id is not our reply.
  h.deliver({jsonrpc:'2.0',id:pendingId,method:'ping'}); await settle();
  assert.equal(JSON.stringify(h.hostReplies.at(-1)),JSON.stringify({jsonrpc:'2.0',id:pendingId,result:{}}),'ping is answered');
  assert.equal(h.$('status-line').classList.contains('error'),false,'the pending save was not resolved by the ping');
  h.deliver({jsonrpc:'2.0',id:999,method:'ui/resource-teardown'}); await settle();
  assert.equal(h.hostReplies.at(-1).id,999);
  const held=h.holdSave; h.holdSave=null; held.forEach(r=>r.reply()); await removal; await settle();
  assert.equal(h.$('status-line').classList.contains('error'),false);
  assert.ok(!h.server.references.some(f=>f.path.includes('中文 视频')),'the held save still completes');

  // ---- Missing file and relink.
  h.server.revision++; h.server.references.splice(1,0,{path:'/fixture/旧位置/镜头.mov',name:'镜头.mov',exists:false,isFile:false});
  await h.$('refresh').click(); await settle();
  const missingRow=h.row('镜头.mov');
  assert.equal(missingRow.classList.contains('missing'),true,'moved originals are flagged on restore');
  const prepared=h.previewRequests.length;
  await missingRow.children[0].click(); await settle();
  assert.equal(h.previewRequests.length,prepared,'a known-missing file is not read');
  assert.match(h.$('preview-message-text').textContent,/原文件已移动或删除/);
  h.pickerFiles=[{path:'/fixture/新位置/镜头.mov',name:'镜头.mov',exists:true,isFile:true}];
  const index=h.rows().indexOf(missingRow);
  await [...h.$('preview-message-actions').children].find(b=>b.textContent==='重新定位…').click(); await settle();
  assert.equal(h.rows()[index].dataset.path,'/fixture/新位置/镜头.mov','relink keeps the row position');
  assert.ok(!h.server.references.some(f=>f.path.includes('旧位置')));
  assert.equal(h.previewRequests.at(-1).path,'/fixture/新位置/镜头.mov','the relinked file opens');
  assert.equal(h.revokedURLs.length,h.blobURLs.length-1,'only the media on screen keeps its Blob URL');
  await h.$('close-preview').click(); await settle();
  assert.equal(h.revokedURLs.length,h.blobURLs.length,'all departed media must revoke their Blob URL');
  assert.ok(!JSON.stringify(h.captures).includes('YWJjZA==') && !JSON.stringify(h.server.saves).includes('YWJjZA=='),'byte payloads must never be saved');
  assert.ok(h.captures.every(c=>!('references' in c)),'diagnostics never carry the list');

  // ---- A failed first read must not let a later save replace the stored list.
  h=boot({failFirstRead:true,list:[existing]});
  await settle();
  assert.equal(h.$('empty-title').textContent,'暂时无法读取参考列表');
  assert.equal(h.$('empty-retry').hidden,false);
  await h.$('choose').click(); await settle();
  assert.ok(h.server.conflicts>=1,'the unread base revision is rejected');
  assert.deepEqual(h.server.references.map(f=>f.path),[existing.path,'/fixture/中文 视频.mp4','/fixture/文档.txt'],'stored items survive and new ones are added');
  assert.equal(h.$('file-count').textContent,'3');

  // ---- Thumbnails: blob images with duration, released with their rows.
  const clip={path:'/fixture/镜头.mp4',name:'镜头.mp4',size:100,exists:true,isFile:true,lastModified:1};
  h=boot({list:[clip,{path:'/fixture/海报.png',name:'海报.png',size:100,exists:true,isFile:true}],thumbnails:true});
  await settle(10);
  const clipThumb=h.row('镜头.mp4').children[0].children[0];
  assert.equal(clipThumb.dataset.state,'ready');
  assert.equal(clipThumb.children[0].tagName,'img');
  assert.match(clipThumb.children[0].src,/^blob:fixture\//);
  assert.equal(clipThumb.querySelector('.duration').textContent,'0:35');
  assert.match(h.row('镜头.mp4').children[0].children[1].textContent,/MP4 · 0:35/);
  const thumbUrl=clipThumb.children[0].src;
  await h.row('镜头.mp4').children[0].click(); await settle();
  assert.equal(h.$('preview-icon').children[0].src,thumbUrl,'the preview header reuses the thumbnail');
  await h.row('镜头.mp4').children[1].click({clientX:1,clientY:1});
  await [...h.$('file-menu').querySelectorAll('button')].find(b=>b.textContent==='移除参考').click(); await settle();
  assert.ok(h.revokedURLs.includes(thumbUrl),'removing a row releases its thumbnail');
  assert.equal(h.$('empty-add').disabled,false);

  // ---- Hosts without a conversation id share one list; the panel says so.
  h=boot({scope:'shared'}); await settle();
  assert.match(h.$('empty-text').textContent,/所有对话共用这份列表/);
  assert.equal(h.$('file-count').title,'所有对话共用这份列表');
  h=boot(); await settle();
  assert.match(h.$('empty-text').textContent,/列表跟随当前对话/);

  const shared={revision:0,references:[],saves:[],conflicts:0,scope:'project',scopeId:'project-A',project:{name:'共享项目',root:'/fixture/project-A'}};
  const panelA=boot({serverState:shared}), panelB=boot({serverState:shared}); await settle();
  assert.match(panelA.$('scope-label').textContent,/共享项目 · 项目共享/);
  assert.match(panelB.$('empty-text').textContent,/同一项目的所有对话共用/);
  await panelA.$('choose').click(); await settle();
  await panelB.pollShelf(); await settle();
  assert.equal(panelB.rows().length,2,'another conversation sees additions when its project shelf refreshes');
  await panelB.pollShelf(); await settle();
  assert.equal(panelB.rows().length,2,'unchanged revision polling keeps the visible shelf');
  await panelB.rows()[0].children[1].click({clientX:1,clientY:1});
  await panelB.$('file-menu').querySelectorAll('button').find(b=>b.textContent==='移除参考').click(); await settle();
  await panelA.pollShelf(); await settle();
  assert.equal(panelA.rows().length,1,'removals are shared too');
  shared.scopeId='project-B';shared.project={name:'另一项目',root:'/fixture/project-B'};shared.revision=0;shared.references=[];
  await panelA.$('refresh').click(); await settle();
  assert.equal(panelA.rows().length,0,'a project switch accepts its own revision and never keeps old rows');
  assert.match(panelA.$('scope-label').textContent,/另一项目/);

  const failedScope={revision:0,references:[],saves:[],conflicts:0,scope:'conversation',scopeId:'fallback',project:null,projectLookup:'failed'};
  const fallback=boot({serverState:failedScope}); await settle();
  assert.equal(fallback.$('scope-label').hidden,false);
  assert.match(fallback.$('scope-label').textContent,/项目识别失败/);
  failedScope.scope='project';failedScope.scopeId='resolved';failedScope.project={name:'已恢复项目',root:'/fixture/recovered'};failedScope.projectLookup='resolved';
  await fallback.pollShelf(); await settle();
  assert.match(fallback.$('scope-label').textContent,/已恢复项目 · 项目共享/);

  console.log('PASS synthetic UI: list scope, thumbnails/grid/list, theme tokens, restore/conflict merge, drag/add, picker, PDF paging, menu keyboard, focus survival, video/text, stale responses, large assembly/cancel, remove/undo, multi-select, host requests, missing/relink');
})().catch(e=>{console.error(e);process.exitCode=1;});
