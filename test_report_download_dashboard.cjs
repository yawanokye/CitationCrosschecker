const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/new_results.html', 'utf8');
const errors = html.slice(html.indexOf('function correctionApiError'), html.indexOf('async function saveCorrectionDecision'));
const download = html.slice(html.indexOf('let packageDownloadInProgress'), html.indexOf('async function deleteNow'));
assert.ok(download.includes('async function downloadPackage'));

function setup({failedAt, status=500, manifest={}, deletion='scheduled-expiry', type='application/zip', size=50, confirmations=[], hold=false}={}) {
    const events=[], timers=[], links=[];
    const nodes={btnDownloadOnly:{textContent:'Download only',disabled:false},btnDownloadDelete:{textContent:'Download and delete',disabled:false},btnDeleteNow:{disabled:false},privacyMessage:{textContent:''}};
    let release;
    const promise = new Promise(resolve=>{release=resolve;});
    const context=vm.createContext({JOB_ID:'test-analysis',latestResults:{privacy:{}},$:id=>nodes[id],
        confirm:()=>confirmations.length ? confirmations.shift() : true,
        setStatus:(message,state)=>events.push({message,state}),
        setTimeout:(callback,ms)=>{timers.push({callback,ms});},
        URL:{createObjectURL:blob=>{assert.equal(blob.size,size);return 'blob:test';},revokeObjectURL:url=>events.push({revoked:url})},
        document:{body:{append:link=>links.push(link)},createElement:()=>({click(){this.clicked=true;},remove(){this.removed=true;}})},
        window:{location:{set href(value){throw new Error('Download must keep the results page open');}}},
        fetch:async url=>{
            events.push({url});
            const stage=url.includes('application-status')?'preflight':'package';
            if(hold&&stage==='preflight') await promise;
            if(failedAt===stage) return {ok:false,status,json:async()=>({error:'The annotated manuscript could not be created. Your analysis has not been deleted.'})};
            return stage==='preflight'?{ok:true,json:async()=>manifest}:{ok:true,headers:{get:key=>key==='Content-Type'?type:key==='X-Content-Deletion'?deletion:null},blob:async()=>({size})};
        }
    });
    vm.runInContext(errors+download,context);
    return {context,nodes,events,timers,links,release};
}

(async()=>{
    for(const failedAt of ['preflight','package']) {
        const state=setup({failedAt});
        await state.context.downloadPackage(false);
        assert.equal(state.links.length,0);
        assert.match(state.events.at(-1).message,/has not been deleted/);
        assert.equal(state.events.at(-1).state,'bad');
        assert.equal(state.nodes.btnDownloadOnly.disabled,false);
        assert.equal(state.nodes.btnDownloadDelete.textContent,'Download and delete');
        assert.equal(state.context.latestResults.privacy.content_deleted,undefined);
    }
    const keep=setup();
    await keep.context.downloadPackage(false);
    assert.match(keep.events.find(e=>e.url?.includes('report-package')).url,/delete_after=0$/);
    assert.equal(keep.links.length,1);
    assert.equal(keep.links[0].download,'CiteIntegrity_Report_test-ana.zip');
    assert.equal(keep.links[0].clicked,true);
    assert.equal(keep.links[0].removed,true);
    assert.equal(keep.nodes.btnDownloadOnly.disabled,false);
    assert.match(keep.nodes.privacyMessage.textContent,/remains available/);
    assert.equal(keep.timers.length,1);
    keep.timers[0].callback();
    assert.equal(keep.events.at(-1).revoked,'blob:test');

    const remove=setup({deletion:'after-download'});
    await remove.context.downloadPackage(true);
    assert.match(remove.events.find(e=>e.url?.includes('report-package')).url,/delete_after=1$/);
    assert.equal(remove.context.latestResults.privacy.content_deleted,true);
    assert.equal(remove.nodes.btnDownloadOnly.disabled,true);
    assert.equal(remove.nodes.btnDownloadDelete.disabled,true);
    assert.equal(remove.nodes.btnDeleteNow.disabled,true);

    const disabledDeletion=setup();
    await disabledDeletion.context.downloadPackage(true);
    assert.equal(disabledDeletion.nodes.btnDownloadOnly.disabled,false);
    assert.match(disabledDeletion.nodes.privacyMessage.textContent,/remains available/);

    const cancel=setup({confirmations:[false]});
    await cancel.context.downloadPackage(true);
    assert.equal(cancel.events.length,0);
    const unplaced=setup({manifest:{unapplied:[{id:'item',reason:'No unique passage'}]},confirmations:[false]});
    await unplaced.context.downloadPackage(false);
    assert.equal(unplaced.links.length,0);
    assert.equal(unplaced.events.filter(e=>e.url).length,1);
    assert.equal(unplaced.nodes.btnDownloadOnly.disabled,false);

    for(const options of [{type:'application/json'},{size:0}]) {
        const invalid=setup(options);
        await invalid.context.downloadPackage(false);
        assert.equal(invalid.links.length,0);
        assert.equal(invalid.events.at(-1).state,'bad');
    }
    const concurrent=setup({hold:true});
    const pending=concurrent.context.downloadPackage(false);
    assert.equal(concurrent.nodes.btnDownloadOnly.disabled,true);
    await concurrent.context.downloadPackage(false);
    assert.equal(concurrent.events.filter(e=>e.url).length,1);
    concurrent.release();
    await pending;
    assert.equal(concurrent.links.length,1);
    assert.equal(concurrent.events.filter(e=>e.url).length,2);
    console.log('Report downloads stay on the dashboard, surface failures, prevent duplicate requests and preserve explicit deletion choices.');
})().catch(error=>{console.error(error);process.exitCode=1;});
