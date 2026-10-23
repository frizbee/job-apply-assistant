"""LinkedIn-only resume widgets; native and ARIA variants are verified explicitly."""
import hashlib
import re
from urllib.parse import urlparse

RESUME_ROOT = '[componentkey="easyApplyUploadedResumeRef"]'
PROOF = 'data-autoapply-resume-sha256'


def linkedin(page):
    return (isinstance(page.url, str) and urlparse(page.url).scheme == 'https'
            and urlparse(page.url).hostname == 'www.linkedin.com')


def resume_group(group):
    return linkedin(group.page) and group.locator(RESUME_ROOT).count() == 1


def digest_for(core, resume):
    digest = hashlib.sha256(resume.read_bytes()).hexdigest()
    if core.SETTINGS.get('cv_hash') and digest != core.SETTINGS['cv_hash']:
        raise ValueError('CV changed; review it before uploading')
    return digest


def selected(root, name):
    native = root.locator('input[type=radio]')
    if native.count() != 1 or not native.is_checked():
        return False
    wrapper = root.locator('[role=radio]')
    if wrapper.count() not in (0, 1):
        return False
    if wrapper.count() == 1:
        if wrapper.get_attribute('aria-checked') != 'true' or wrapper.get_attribute('aria-label') != name:
            return False
    native_aria = native.get_attribute('aria-checked')
    if native_aria is not None and native_aria != 'true':
        return False
    label = native.get_attribute('aria-label') or (wrapper.get_attribute('aria-label') if wrapper.count() else '')
    return label == name


def verified_group(group, core):
    try:
        resume = core.HOME / core.PROFILE['resume_file']
        digest = digest_for(core, resume)
        root = group.locator(RESUME_ROOT)
        return (root.count() == 1 and root.get_attribute(PROOF) == digest and selected(root, resume.name)
                and group.locator('input[type=radio]').count() == root.locator('input[type=radio]').count()
                and group.locator('[role=radio]').count() == root.locator('[role=radio]').count()
                and group.locator('input[type=radio]:checked').count() == 1
                and group.locator('[role=radio][aria-checked=true]').count() == root.locator('[role=radio]').count())
    except (OSError, KeyError, ValueError):
        return False


def prepare_resume(page, form, core):
    """Upload the actual reviewed local file; a matching saved filename isn't proof."""
    if not linkedin(page):
        return []
    upload = form.get_by_role('button', name=re.compile(r'^Upload resume$', re.I))
    files = form.locator('input[type=file]')
    if not upload.count() and not files.count():
        return []
    resume = core.HOME / core.PROFILE['resume_file']
    stage = 'validating CV'
    try:
        digest = digest_for(core, resume)
        if resume.stat().st_size > 2 * 1024 * 1024:
            return ['LinkedIn resume exceeds the 2 MB upload limit']
        root = form.locator(RESUME_ROOT)
        if root.count() == 1 and root.get_attribute(PROOF) == digest and selected(root, resume.name):
            return []
        candidates = []
        for field in files.all():
            label = ' '.join([core.question_of(field), field.get_attribute('name') or '', field.get_attribute('id') or ''])
            if not re.search(r'cover', label, re.I) and (re.search(r'resume|\bcv\b', label, re.I) or upload.count() == 1):
                candidates.append(field)
        if not candidates and not upload.count():
            return []  # A cover-letter or unrelated file input is not a resume widget.
        if len(candidates) == 1:
            stage = 'setting the native resume input'
            field = candidates[0]
            if field.get_attribute(PROOF) == digest and field.evaluate('(e,n)=>e.files.length===1 && e.files[0].name===n', resume.name):
                return []
            field.set_input_files(str(resume))
            if field.evaluate('(e,n)=>e.files.length===1 && e.files[0].name===n', resume.name):
                field.evaluate('(e,p)=>e.setAttribute(p[0],p[1])', [PROOF, digest])
        elif not candidates and upload.count() == 1:
            stage = 'waiting for the upload file chooser'
            # SDUI creates its file input only after the upload button is clicked.
            with page.expect_file_chooser(timeout=5000) as pending:
                core.click_form_control(upload)
            chooser = pending.value
            stage = 'verifying the upload destination'
            owner = chooser.element.owner_frame()
            if owner is None or not core.safe_https_url(owner.url) or urlparse(owner.url).hostname != 'www.linkedin.com':
                return ['Cannot verify the LinkedIn resume upload destination']
            chooser.set_files(str(resume))
        else:
            return ['LinkedIn resume upload control is missing or ambiguous']
        stage = 'verifying resume selection'
        selection_requested = False
        for _ in range(24):
            if core.STOP.is_set():
                raise core.Stopped
            page.wait_for_timeout(250)
            root = form.locator(RESUME_ROOT)
            if root.count() == 1:
                radio = root.get_by_role('radio', name=resume.name, exact=True)
                if radio.count() == 1 and not selected(root, resume.name) and not selection_requested:
                    core.click_form_control(radio)
                    selection_requested = True
                if selected(root, resume.name):
                    root.evaluate('(e,p)=>e.setAttribute(p[0],p[1])', [PROOF, digest])
                    return []
            elif len(candidates) == 1 and candidates[0].count() == 1:
                if candidates[0].get_attribute(PROOF) == digest:
                    return []
        return ['Resume uploaded, but LinkedIn did not confirm its selection']
    except ValueError as e:
        return [str(e)]
    except Exception as e:
        if core.browser_closed(e):
            raise core.BrowserClosed() from None
        core.METRICS.mark('errors')
        return [f'LinkedIn resume needs review ({stage}): {e.__class__.__name__}']
