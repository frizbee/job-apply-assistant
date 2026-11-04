"""Narrow manual authentication steps; never auto-click or bypass verification."""

BUK_EMAIL_STEP_JS = r"""() => {
    if (location.protocol !== 'https:' || location.hostname !== 'buk.buk.cl' ||
        !/^\/seleccions\/[a-f0-9]{32,256}\/postular\/?$/.test(location.pathname)) return false;
    if (window.__autoapplyBukEmailStepInstalled) return true;
    window.__autoapplyBukEmailStepInstalled = true;
    const handler = event => {
        if (!event.isTrusted || window.__autoapplyManualAllowSubmit !== false) return;
        const button = event.target.closest('button,input[type="submit"]');
        const form = button?.form;
        if (!form || form.id !== 'invisible-recaptcha-form' || form.method.toLowerCase() !== 'post') return;
        const target = new URL(button.hasAttribute('formaction') ? button.formAction : form.action, location.href);
        if (target.origin !== location.origin || target.username || target.password || target.search ||
            target.pathname !== location.pathname.replace(/\/$/, '') + '/verificar') return;
        const email = form.querySelector('input#login_form_email[type="email"][name="login_form[email]"]');
        const terms = form.querySelector('input#login_form_accept_terms[type="checkbox"]');
        if (!email || !email.value.trim() || !email.checkValidity() || !terms?.checked) return;
        // Never turn this exemption into permission for a full application form.
        const fields = [...form.elements].filter(e => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName));
        if (fields.some(e => e.type !== 'hidden' && e !== email && e !== terms &&
            !(e.tagName === 'TEXTAREA' && e.name === 'g-recaptcha-response'))) return;
        window.__autoapplyManualClickUsed = false;
        window.__autoapplyManualSubmitUsed = false;
        window.__autoapplyManualAllowSubmit = true;
        // Only the current physical click/event dispatch is authorized. The site's
        // normal reCAPTCHA callback may proceed; we neither supply nor solve it.
        setTimeout(() => { window.__autoapplyManualAllowSubmit = false; }, 0);
    };
    window.addEventListener('click', handler, true);
    return true;
}"""
