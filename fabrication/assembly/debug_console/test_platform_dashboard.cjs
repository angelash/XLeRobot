const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/platform_static/studio.js','utf8');
const visuals=fs.readFileSync(__dirname+'/platform_static/diagrams.js','utf8');
const context=vm.createContext({module:{exports:{}},console});
vm.runInContext(visuals+'\n'+source,context);
const studio=context.module.exports;
assert.equal(studio.esc('<img src=x onerror=alert(1)>'),'&lt;img src=x onerror=alert(1)&gt;');
const unsafe=studio.markdown('# Manual\n<script>alert(1)</script>\n[bad](javascript:alert)\n[link](https://github.com/example)\n```html\n<img onerror=x>\n```');
assert(!unsafe.includes('<script>')&&!unsafe.includes('<img'));
assert(unsafe.includes('rel="noopener noreferrer"')&&!unsafe.includes('href="javascript:'));
assert.match(studio.sessionInfo({upstream_connected:true,runtime:{controller:{controller:'online',paused:true,pause_reason:'handpad absent'}}}).text,/接管保留/);
assert.match(studio.sessionInfo({upstream_connected:false}).detail,/不可确认/);
const nodes={'sandbox-side':{value:'both'},'sandbox-mode':{value:'position'}};
context.document={getElementById:id=>nodes[id]};
let input=studio.sandboxInput('extend');assert.equal(input.axes[1],-.8);assert.equal(input.axes[3],-.8);
nodes['sandbox-mode'].value='wrist';input=studio.sandboxInput('up');assert.deepEqual(Array.from(input.buttons),[4,5]);
nodes['sandbox-side'].value='right';input=studio.sandboxInput('grip');assert.equal(input.axes[5],1);assert.equal(input.axes[4],0);
for(const kind of ['bringup','gamepad','limits','data','vision','learning','agent','architecture','vr','fabrication','simulation']){
  context.diagramKind=kind;const svg=vm.runInContext('StudioVisuals.diagram(diagramKind)',context);assert(svg.startsWith('<svg')&&svg.includes('role="img"'));
}
// A page switch hides the controller container; it must never remove a live iframe.
assert(!source.includes("/api/controller/stop',{}"));
assert(source.includes("$('control-page').hidden=route!=='control'"));
assert.match(source,/if\(!frame\)\{\s*frame=document.createElement\('iframe'\)/);
console.log('Platform UI PASS: escaped sources, session labels, sandbox mappings, illustrated guides, retained controller frame');
