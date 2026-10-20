const STORAGE_KEY='openngs.studio.panels.v1';
const LIMITS={left:{min:190,max:440,size:274},right:{min:240,max:500,size:322},bottom:{min:120,max:500,size:218}};
const SIDES=['left','right'];
const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));

export function panelPreferences(raw,width=1280){
 const result={version:1};
 for(const [name,limits] of Object.entries(LIMITS)){
  const fallback=name==='bottom'?(width<=800?210:218):name==='left'?(width<=800?205:width<=1150?240:274):(width<=800?240:width<=1150?285:322);
  result[name]={size:Number.isFinite(raw?.[name]?.size)?clamp(raw[name].size,limits.min,limits.max):fallback,collapsed:raw?.[name]?.collapsed===true};
 }
 return result;
}

// Keep preferred sizes intact when the viewport temporarily becomes smaller.
export function panelLayout(preferences,width,height){
 const mobile=width<=650,rail=34,minimumCanvas=180;
 let left=preferences.left.collapsed?rail:preferences.left.size;
 let right=preferences.right.collapsed?rail:preferences.right.size;
 if(!mobile){
  let overflow=Math.max(0,left+right+minimumCanvas-width);
  for(const name of ['right','left']){
   const current=name==='left'?left:right;
   const minimum=preferences[name].collapsed?rail:LIMITS[name].min;
   const reduction=Math.min(overflow,Math.max(0,current-minimum));
   if(name==='left')left-=reduction;else right-=reduction;
   overflow-=reduction;
  }
 }
 const bottomMax=Math.max(37,Math.min(500,height-94-25-150));
 const bottom=preferences.bottom.collapsed?37:Math.min(preferences.bottom.size,bottomMax);
 return {mobile,left,right,bottom,bottomMax};
}

export function installPanels({onLayout=()=>{}}={}){
 const $=id=>document.getElementById(id),body=document.body;
 let saved;
 try{saved=JSON.parse(localStorage.getItem(STORAGE_KEY));}catch{}
 const preferences=panelPreferences(saved,window.innerWidth);
 let mobileOpen=null,frame=0,lastMobile=window.innerWidth<=650;
 const titles={left:'library',right:'inspector',bottom:'execution panel'};
 const content={left:$('leftPanelContent'),right:$('rightPanelContent'),bottom:$('bottomContent')};
 const handles={left:$('leftResize'),right:$('rightResize'),bottom:$('bottomResize')};
 const collapse={left:$('collapseLeft'),right:$('collapseRight'),bottom:$('collapseBottom')};
 const restore={left:$('restoreLeft'),right:$('restoreRight')};
 const mobileToggle={left:$('mobileLibraryToggle'),right:$('mobileInspectorToggle')};
 let layout;
 const persist=()=>{try{localStorage.setItem(STORAGE_KEY,JSON.stringify(preferences));}catch{}};
 const scheduleLayout=()=>{if(!frame)frame=requestAnimationFrame(()=>{frame=0;onLayout();});};
 const focusPanel=name=>{(name==='left'?$('librarySearch'):name==='right'?$('collapseRight'):$('bottomTabs').querySelector('.active'))?.focus({preventScroll:true});};
 function apply(){
  layout=panelLayout(preferences,window.innerWidth,window.innerHeight);
  if(layout.mobile!==lastMobile){mobileOpen=null;lastMobile=layout.mobile;}
  for(const name of Object.keys(LIMITS)){
   body.style.setProperty('--'+name,layout[name]+'px');
   const expanded=name==='bottom'?!preferences.bottom.collapsed:layout.mobile?mobileOpen===name:!preferences[name].collapsed;
   content[name].hidden=!expanded;content[name].inert=!expanded;
   const action=(expanded?'Minimise ':'Restore ')+titles[name];
   collapse[name].setAttribute('aria-expanded',String(expanded));
   collapse[name].setAttribute('aria-label',action);
   collapse[name].title=action+' · Alt+'+(name==='left'?1:name==='right'?2:3);
   handles[name].setAttribute('aria-valuemin',String(name==='bottom'?37:LIMITS[name].min));
   handles[name].setAttribute('aria-valuemax',String(name==='bottom'?layout.bottomMax:LIMITS[name].max));
   handles[name].setAttribute('aria-valuenow',String(Math.round(layout[name])));
   handles[name].setAttribute('aria-valuetext',Math.round(layout[name])+' pixels'+(expanded?'':' · minimised'));
   handles[name].setAttribute('aria-keyshortcuts',name==='bottom'?'ArrowUp ArrowDown Home End Enter':'ArrowLeft ArrowRight Home End Enter');
   handles[name].title='Drag to resize. Arrow keys resize; Shift + arrow moves faster. Enter minimises or restores.';
   if(name!=='bottom'){
    $(''+name+'PanelRail').hidden=layout.mobile||expanded;
    body.classList.toggle(name+'-collapsed',!layout.mobile&&!expanded);
    restore[name].setAttribute('aria-expanded',String(expanded));
    mobileToggle[name].setAttribute('aria-expanded',String(expanded));
    mobileToggle[name].setAttribute('aria-controls',name+'PanelContent');
    mobileToggle[name].setAttribute('aria-label',(expanded?'Close ':'Open ')+titles[name]);
   }
  }
  body.classList.toggle('library-open',layout.mobile&&mobileOpen==='left');
  body.classList.toggle('inspector-open',layout.mobile&&mobileOpen==='right');
  body.classList.toggle('bottom-collapsed',preferences.bottom.collapsed);
  $('panelBackdrop').hidden=!layout.mobile||!mobileOpen;
  $('collapseBottom').textContent=preferences.bottom.collapsed?'⌃':'⌄';
  scheduleLayout();
 }
 function setCollapsed(name,collapsed,focus=false){
  const heldFocus=content[name].contains(document.activeElement);
  if(layout.mobile&&SIDES.includes(name))mobileOpen=collapsed?null:name;
  else{preferences[name].collapsed=collapsed;persist();}
  apply();
  if(collapsed&&(heldFocus||focus)){
   (name==='bottom'?collapse.bottom:layout.mobile?mobileToggle[name]:restore[name]).focus({preventScroll:true});
  }else if(!collapsed&&focus)focusPanel(name);
 }
 function toggle(name){const collapsed=name==='bottom'?preferences.bottom.collapsed:layout.mobile?mobileOpen!==name:preferences[name].collapsed;setCollapsed(name,!collapsed,true);}
 function reveal(name,focus=false){setCollapsed(name,false,focus);}
 function closeMobile(focus=false){if(!mobileOpen)return;setCollapsed(mobileOpen,true,focus);}
 for(const name of Object.keys(LIMITS)){
  collapse[name].onclick=()=>toggle(name);
  restore[name]?.addEventListener('click',()=>reveal(name,true));
  mobileToggle[name]?.addEventListener('click',()=>toggle(name));
 }
 $('panelBackdrop').onclick=()=>closeMobile(true);
 $('bottomTabs').addEventListener('click',event=>{if(event.target.closest('[data-bottom]'))reveal('bottom');});
 for(const button of [...Object.values(collapse),...Object.values(restore),...Object.values(mobileToggle)]){
  // Keep the graph's Space-to-pan listener from consuming native button activation.
  button.addEventListener('keydown',event=>{if(event.code==='Space')event.stopPropagation();});
 }
 function resize(name,size){
  preferences[name].collapsed=false;
  preferences[name].size=clamp(size,LIMITS[name].min,name==='bottom'?Math.max(LIMITS.bottom.min,layout.bottomMax):LIMITS[name].max);
  apply();
 }
 for(const [name,handle] of Object.entries(handles)){
  const vertical=name==='bottom',direction=name==='left'?1:-1;
  handle.addEventListener('pointerdown',event=>{
   if(event.button!==0||layout.mobile&&!vertical)return;
   event.preventDefault();handle.focus({preventScroll:true});
   const start=vertical?event.clientY:event.clientX,original=layout[name];
   let moved=false;
   handle.setPointerCapture(event.pointerId);body.classList.add('panel-resizing');
   const move=e=>{if(e.pointerId!==event.pointerId)return;const delta=((vertical?e.clientY:e.clientX)-start)*direction;if(Math.abs(delta)>2)moved=true;if(moved)resize(name,original+delta);};
   const stop=e=>{
    if(e.pointerId!==event.pointerId)return;
    handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',stop);handle.removeEventListener('pointercancel',stop);handle.removeEventListener('lostpointercapture',stop);
    body.classList.remove('panel-resizing');if(handle.hasPointerCapture(event.pointerId))handle.releasePointerCapture(event.pointerId);persist();
   };
   handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',stop);handle.addEventListener('pointercancel',stop);handle.addEventListener('lostpointercapture',stop);
  });
  handle.addEventListener('keydown',event=>{
   const arrows=vertical?['ArrowUp','ArrowDown']:['ArrowLeft','ArrowRight'];
   if(![...arrows,'Home','End','Enter'].includes(event.key))return;
   event.preventDefault();event.stopPropagation();
   if(event.key==='Enter'){toggle(name);return;}
   const next=event.key==='Home'?LIMITS[name].min:event.key==='End'?(vertical?layout.bottomMax:LIMITS[name].max):layout[name]+(arrows.indexOf(event.key)?1:-1)*direction*(event.shiftKey?40:10);
   resize(name,next);persist();
  });
 }
 window.addEventListener('keydown',event=>{
  if(document.querySelector('dialog[open]'))return;
  if(event.key==='Escape'&&layout.mobile&&mobileOpen){event.preventDefault();closeMobile(true);return;}
  if(document.activeElement?.isContentEditable||/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName))return;
  if(event.altKey&&!event.ctrlKey&&!event.metaKey&&['Digit1','Digit2','Digit3'].includes(event.code)){event.preventDefault();toggle({Digit1:'left',Digit2:'right',Digit3:'bottom'}[event.code]);}
 });
 window.addEventListener('resize',apply);
 apply();
 return {reveal,closeMobile,toggle,get mobile(){return layout.mobile;}};
}
