"""Native validity plus committed selections in custom application widgets.

Never treat an empty React Select search input as the selected answer. Its
native required proxy remains a real validation constraint, not a field to hide.
"""

NATIVE_FORM_DIAGNOSTICS_JS = r"""elements => {
    const clean=s=>(s||'').replace(/\s+/g,' ').trim();
    const hidden=e=>{
        for(let p=e;p;p=p.parentElement) {
            const style=getComputedStyle(p);
            if(p.hidden||p.inert||style.display==='none'||/hidden|collapse/.test(style.visibility)) return true;
        }
        return false;
    };
    const accessibleName=e=>{
        const ids=(e.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
        const labelled=clean(ids.map(id=>e.ownerDocument.getElementById(id)?.textContent||'').join(' '));
        const labels=clean([...(e.labels||[])].map(l=>l.textContent).join(' '));
        return labelled||labels||clean(e.getAttribute('aria-label'));
    };
    const widget=e=>{
        const shell=e.closest('.select-shell,.select__container')||e.closest('.select__control')?.parentElement;
        if(!shell) return null;
        const combos=[...shell.querySelectorAll('input[role="combobox"]')];
        if(combos.length!==1) return null;
        const combo=combos[0];
        const proxy=e!==combo&&e.matches('input[required][aria-hidden="true"][tabindex="-1"]');
        if(e!==combo&&!proxy) return null;
        const control=combo.closest('.select__control');
        if(!control) return null;
        const single=[...control.querySelectorAll('.select__single-value')].filter(n=>!hidden(n)&&clean(n.textContent));
        const multi=[...control.querySelectorAll('.select__multi-value__label')].filter(n=>!hidden(n)&&clean(n.textContent));
        const selected=(single.length===1&&!multi.length)||(!single.length&&multi.length>0);
        return {combo,proxy,selected,ambiguous:single.length>1||(single.length>0&&multi.length>0)};
    };
    const labelFor=(e,w)=>{
        const target=w?.combo||e;
        const direct=accessibleName(target);
        if(direct) return direct;
        const group=target.closest('fieldset,[role="radiogroup"]');
        const legend=group?.querySelector('legend');
        return clean(legend?.textContent)||accessibleName(group||target)||
            clean(target.getAttribute('placeholder'))||'Required field';
    };
    const checkboxSelectionGroup=e=>{
        if(!e.matches('input[type="checkbox"],fieldset,[role="group"]')) return null;
        const group=e.closest('fieldset,[role="group"]');
        if(!group) return null;
        const boxes=[...group.querySelectorAll('input[type="checkbox"]')];
        if(boxes.length<2||boxes.some(box=>box.closest('fieldset,[role="group"]')!==group)) return null;
        const question=clean(group.querySelector(':scope > legend')?.textContent)||accessibleName(group);
        const multiple=/\b(?:select|choose|check|mark)\s+all\s+(?:that\s+apply|applicable)\b|\b(?:select|choose|check)\s+(?:at\s+least\s+one|one\s+or\s+more)\b/i;
        // Do not turn separate required legal agreements into an any-one rule.
        const consent=/\b(?:consent|agree|acknowledge|certify|privacy|terms|legal)\b/i;
        if(!multiple.test(question)||consent.test(question)||boxes.some(box=>consent.test(accessibleName(box)))) return null;
        const names=new Set(boxes.map(box=>box.name));
        if(names.size!==1||!boxes[0].name) return null;
        return {group,boxes,question};
    };
    const issues=[];
    const inspectedGroups=new Set();
    for(const e of elements) {
        if(e.disabled||e.readOnly||e.type==='hidden'||hidden(e)) continue;
        const checkboxGroup=checkboxSelectionGroup(e);
        if(checkboxGroup) {
            const {group,boxes,question}=checkboxGroup;
            if(inspectedGroups.has(group)) continue;
            inspectedGroups.add(group);
            const active=boxes.filter(box=>!box.disabled&&box.getAttribute('aria-disabled')!=='true'&&!hidden(box));
            const required=group.getAttribute('aria-required')==='true'||active.some(box=>box.required||box.getAttribute('aria-required')==='true');
            let reason=null;
            if(group.getAttribute('aria-invalid')==='true'||active.some(box=>box.getAttribute('aria-invalid')==='true'||(box.validity&&!box.validity.valid&&!box.validity.valueMissing))) reason='invalid';
            else if(active.some(box=>box.indeterminate||(box.hasAttribute('aria-checked')&&box.getAttribute('aria-checked')!==String(box.checked)))) reason='selection_ambiguous';
            else if(required&&!active.some(box=>box.checked)) reason='checkbox_selection_required';
            if(reason) {
                const detail={invalid:'the site reports an invalid value',selection_ambiguous:'the selected options cannot be verified',
                    checkbox_selection_required:'select at least one applicable option'}[reason];
                issues.push({label:question,reason,id:group.id||'',message:question+' — '+detail});
            }
            continue;
        }
        const w=widget(e);
        const required=e.required||e.getAttribute('aria-required')==='true';
        let reason=null;
        if(e.getAttribute('aria-invalid')==='true'||w?.combo.getAttribute('aria-invalid')==='true') reason='invalid';
        else if(w?.ambiguous) reason='selection_ambiguous';
        else if(w?.proxy&&e.validity&&!e.validity.valid) {
            reason=w.selected?'selection_not_accepted':'selection_required';
        }
        else if(e.validity&&!e.validity.valid) reason=e.validity.valueMissing?'required':'invalid';
        else if(required) {
            if(w) {
                if(!w.selected) reason='selection_required';
            } else if(e.type==='checkbox') {
                if(!e.checked) reason='required';
            } else if(e.type==='radio') {
                const selected=e.name?[...e.getRootNode().querySelectorAll('input[type="radio"]')].some(r=>r.name===e.name&&r.form===e.form&&r.checked):e.checked;
                if(!selected) reason='required';
            } else if(e.type==='file') {
                if(!e.files?.length) reason='required';
            } else if(e.getAttribute('role')==='radiogroup') {
                if(!e.querySelector('[aria-checked="true"],input:checked')) reason='required';
            } else if(e.matches('fieldset,[role="group"]')&&
                      e.querySelectorAll('input[type="checkbox"]').length===1&&
                      !e.querySelector('input:not([type="hidden"]):not([type="checkbox"]),textarea,select,[role="combobox"]')) {
                // Greenhouse puts aria-required on a fieldset as well as required
                // on its sole acknowledgement checkbox. A checked native value
                // satisfies that wrapper; fieldsets have no aria-valuetext.
                const box=e.querySelector('input[type="checkbox"]');
                if(box.disabled||box.getAttribute('aria-disabled')==='true'||!box.checked) reason='required';
                else if(box.indeterminate||(box.hasAttribute('aria-checked')&&box.getAttribute('aria-checked')!==String(box.checked)))
                    reason='selection_ambiguous';
            } else if(e.matches('.file-upload[role="group"]')) {
                // Greenhouse replaces the file input after a completed upload.
                // Its required group has neither value nor aria-valuetext.
                const receipts=[...e.querySelectorAll('.file-upload__filename')].filter(n=>!hidden(n));
                const receipt=receipts.length===1?receipts[0]:null;
                const filename=clean(receipt?.querySelector('p')?.textContent);
                const remove=receipt?.querySelector('button[aria-label="Remove file"]');
                if(!filename||!remove||remove.disabled||hidden(remove)) reason='upload_required';
            } else if('value' in e) {
                if(!clean(e.value)) reason='required';
            } else if(!clean(e.getAttribute('aria-valuetext'))) reason='required';
        }
        if(!reason) continue;
        const label=labelFor(e,w);
        const detail={required:'complete this required field',invalid:'the site reports an invalid value',
            selection_required:'choose an option from the dropdown list',
            selection_not_accepted:'the site has not accepted this selection; choose the option again',
            selection_ambiguous:'the selected option cannot be identified uniquely',
            upload_required:'upload the file and wait for the upload confirmation'}[reason];
        issues.push({label,reason,id:(w?.combo||e).id||'',message:label+' — '+detail});
    }
    return issues;
}"""
