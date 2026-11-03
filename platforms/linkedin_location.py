"""Reviewed residence city selection for LinkedIn's portalled typeahead."""
import json
from urllib.parse import urlparse

PROOF = 'data-autoapply-city-selection'


def normalized(value):
    return ' '.join(value.split()).casefold()


def city_field(field, question):
    url = field.page.url
    return (isinstance(url, str) and urlparse(url).hostname == 'www.linkedin.com'
            and urlparse(url).scheme == 'https'
            and normalized(question).rstrip('*').strip() in
            ('location (city)', 'city', 'current city', 'city of residence', 'location'))


def autocomplete(field, question):
    return city_field(field, question) and field.get_attribute('aria-autocomplete') == 'list'


def matching_location(label, city, country):
    """Permit extra administrative levels, never another city or country."""
    desired = [normalized(part) for part in city.split(',') if part.strip()]
    actual = [normalized(part) for part in label.split(',') if part.strip()]
    country = normalized(country)
    if not desired or len(actual) < 2 or not country or actual[0] != desired[0] or actual[-1] != country:
        return False
    if len(desired) > 1 and desired[-1] == country:
        desired = desired[:-1]
    # Any province explicitly supplied in the reviewed profile must also match.
    position = 0
    for part in desired:
        try:
            position = actual.index(part, position) + 1
        except ValueError:
            return False
    return True


def results(field):
    ids = (field.get_attribute('aria-controls') or field.get_attribute('aria-owns') or '').split()
    if len(ids) != 1:
        return None
    return field.page.locator(f'[id={json.dumps(ids[0])}]:visible')


def confirmed(field, core):
    try:
        proof = json.loads(field.get_attribute(PROOF) or '{}')
    except ValueError:
        return False
    popup = results(field)
    return (proof.get('city') == core.PROFILE.get('city')
            and proof.get('country') == core.PROFILE.get('country')
            and proof.get('value') == field.input_value()
            and matching_location(field.input_value(), proof.get('city', ''), proof.get('country', ''))
            and field.get_attribute('aria-invalid') != 'true'
            and (popup is None or not popup.locator('[role="option"]:visible').count()))


def fill(field, candidates, core):
    if confirmed(field, core):
        return True
    city, country = core.PROFILE.get('city', ''), core.PROFILE.get('country', '')
    if not city or not country or city not in candidates:
        return False
    previous = field.input_value().strip()
    if previous and normalized(previous) != normalized(city) and not matching_location(previous, city, country):
        return False  # Do not silently replace another prefilled residence.
    field.fill(city)
    options = None
    for _ in range(32):
        if core.STOP.is_set():
            raise core.Stopped
        popup = results(field)
        if popup is not None and popup.count() == 1:
            options = popup.locator('[role="option"]:visible')
            if options.count():
                break
        field.page.wait_for_timeout(250)
    if options is None or not options.count():
        return False
    labels = options.all_inner_texts()
    matches = [i for i, label in enumerate(labels) if matching_location(label, city, country)]
    # Duplicate labels are the same displayed location; distinct provinces are
    # ambiguous and require review rather than choosing the first suggestion.
    if not matches or len({normalized(labels[i]) for i in matches}) != 1:
        return False
    chosen = labels[matches[0]]
    core.click_form_control(options.nth(matches[0]))
    for _ in range(16):
        field.page.wait_for_timeout(250)
        popup = results(field)
        if normalized(field.input_value()) == normalized(chosen) and (popup is None or not popup.locator('[role="option"]:visible').count()):
            field.evaluate('''(e,p)=>{
                e.setAttribute(p[0],JSON.stringify(p[1]));
                e.addEventListener('input',()=>e.removeAttribute(p[0]),{once:true});
            }''', [PROOF, {'city': city, 'country': country, 'value': field.input_value()}])
            return confirmed(field, core)
    return False
