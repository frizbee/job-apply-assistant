"""User-approved search policy from AGENTS.md; no submission authorization here."""
import hashlib
from pathlib import Path
import re

VERSION = 'agents-70-v1'
TITLES = [
    'Senior Ruby on Rails Developer', 'Senior Ruby on Rails Engineer', 'Senior Ruby Developer',
    'Lead Ruby on Rails Developer', 'Ruby on Rails Technical Lead', 'Senior Backend Engineer',
    'Senior Backend Developer', 'Lead Software Engineer', 'Senior Full Stack Developer',
]
COVER_LETTER = '''I’m a Senior / Lead Software Engineer with 17+ years of experience, including 14+ years with Ruby on Rails. My background includes multi-tenant SaaS platforms, payment integrations, APIs, and production infrastructure.

At CoachVantage, I led architecture and development for a platform serving 6,800+ users and guided a distributed team of five developers. I combine hands-on engineering with technical leadership, with a focus on reliable systems, performance, and maintainable code.

I’m based in Buenos Aires and would welcome the opportunity to discuss how my experience could support your team.

Best regards,
Andrey Antonov'''

# Country/territory queries cover the non-Asian world, with broad/global queries
# for cross-border hiring. LinkedIn may not support every native label: persist
# those failures rather than silently substituting locations or claiming coverage.
EUROPE = '''Albania|Andorra|Austria|Belarus|Belgium|Bosnia and Herzegovina|Bulgaria|Croatia|Czechia|Denmark|Estonia|Finland|France|Germany|Greece|Hungary|Iceland|Ireland|Italy|Kosovo|Latvia|Liechtenstein|Lithuania|Luxembourg|Malta|Moldova|Monaco|Montenegro|Netherlands|North Macedonia|Norway|Poland|Portugal|Romania|San Marino|Serbia|Slovakia|Slovenia|Spain|Sweden|Switzerland|Ukraine|United Kingdom|Vatican City|Faroe Islands|Gibraltar|Guernsey|Isle of Man|Jersey|Åland Islands'''.split('|')
AMERICAS = '''United States|Canada|Mexico|Argentina|Bolivia|Brazil|Chile|Colombia|Ecuador|Guyana|Paraguay|Peru|Suriname|Uruguay|Venezuela|Belize|Costa Rica|El Salvador|Guatemala|Honduras|Nicaragua|Panama|Antigua and Barbuda|Bahamas|Barbados|Cuba|Dominica|Dominican Republic|Grenada|Haiti|Jamaica|Saint Kitts and Nevis|Saint Lucia|Saint Vincent and the Grenadines|Trinidad and Tobago|Puerto Rico|Aruba|Curaçao|Sint Maarten|Bermuda|Cayman Islands|Greenland|French Guiana|Guadeloupe|Martinique|Anguilla|British Virgin Islands|United States Virgin Islands|Turks and Caicos Islands|Montserrat|Bonaire|Saint Barthélemy|Saint Martin|Saint Pierre and Miquelon|Falkland Islands'''.split('|')
AFRICA = '''Algeria|Angola|Benin|Botswana|Burkina Faso|Burundi|Cabo Verde|Cameroon|Central African Republic|Chad|Comoros|Democratic Republic of the Congo|Republic of the Congo|Djibouti|Egypt|Equatorial Guinea|Eritrea|Eswatini|Ethiopia|Gabon|Gambia|Ghana|Guinea|Guinea-Bissau|Ivory Coast|Kenya|Lesotho|Liberia|Libya|Madagascar|Malawi|Mali|Mauritania|Mauritius|Morocco|Mozambique|Namibia|Niger|Nigeria|Rwanda|São Tomé and Príncipe|Senegal|Seychelles|Sierra Leone|Somalia|South Africa|South Sudan|Sudan|Tanzania|Togo|Tunisia|Uganda|Zambia|Zimbabwe|Réunion|Mayotte|Western Sahara|Saint Helena'''.split('|')
OCEANIA = '''Australia|New Zealand|Fiji|Kiribati|Marshall Islands|Micronesia|Nauru|Palau|Papua New Guinea|Samoa|Solomon Islands|Tonga|Tuvalu|Vanuatu|Cook Islands|Niue|New Caledonia|French Polynesia|Guam|American Samoa|Northern Mariana Islands|Tokelau|Wallis and Futuna|Norfolk Island|Pitcairn Islands'''.split('|')
ASIA = '''Afghanistan|Bahrain|Bangladesh|Bhutan|Brunei|Cambodia|China|Hong Kong|India|Indonesia|Iran|Iraq|Israel|Japan|Jordan|Kuwait|Kyrgyzstan|Laos|Lebanon|Macao|Malaysia|Maldives|Mongolia|Myanmar|Nepal|North Korea|Oman|Pakistan|Palestine|Philippines|Qatar|Saudi Arabia|Singapore|South Korea|Sri Lanka|Syria|Taiwan|Tajikistan|Thailand|Timor-Leste|Turkmenistan|United Arab Emirates|Uzbekistan|Vietnam|Yemen'''.split('|')
AMBIGUOUS = ['Russia', 'Turkey', 'Türkiye', 'Kazakhstan', 'Georgia', 'Armenia', 'Azerbaijan', 'Cyprus']
LOCATIONS = list(dict.fromkeys(['Argentina', 'Latin America', 'United States', 'Canada',
                               'European Union', 'European Economic Area', 'United Kingdom', 'Australia', 'New Zealand',
                               'Worldwide', 'Europe', 'Africa', 'Caribbean', 'Oceania',
                               'Antarctica', 'South Georgia and the South Sandwich Islands', 'Bouvet Island',
                               *EUROPE, *AMERICAS, *AFRICA, *OCEANIA, *AMBIGUOUS]))
REASSESSABLE = {'metadata_manual', 'title_not_relevant', 'relevance_manual', 'required_stack_incompatible',
               'specialized_role', 'excluded_technology', 'title_keyword_excluded', 'description_excluded',
               'posting_too_old', 'not_remote', 'resume_match_below_threshold', 'no_easy_apply', 'no_apply_button',
               'job_closed', 'title_excluded', 'company_excluded'}


def reassess(core, previous):
    return active(core) and previous.get('status') in REASSESSABLE


def active(core):
    return core.CFG.get('application_policy', {}).get('enabled', False) and core.SETTINGS.get('application_policy_version') == VERSION


def activate(core):
    setting = core.CFG.get('application_policy', {})
    if not isinstance(setting, dict) or type(setting.get('enabled', False)) is not bool:
        raise ValueError('application_policy.enabled must be boolean')
    if not setting.get('enabled', False):
        return
    cv = core.ROOT / 'CV_Andre2026.pdf'
    if not cv.is_file():
        raise ValueError('AGENTS.md requires CV_Andre2026.pdf in the project root')
    digest = hashlib.sha256(cv.read_bytes()).hexdigest()
    if core.SETTINGS.get('cv_profile') is not None and core.SETTINGS.get('cv_hash') != digest:
        raise ValueError('Root CV differs from reviewed profile; review the root CV before starting')
    core.SETTINGS.update(resume=str(cv), application_policy_version=VERSION)
    core.PROFILE.update(resume_file=str(cv), cover_letter=COVER_LETTER)
    core.CFG['search'].update(keywords=list(TITLES), locations=list(LOCATIONS), remote_only=True,
                              posted_within_hours=None, pages_per_search=None, experience_levels=[])
    core.CFG.setdefault('resume_match', {}).update(enabled=True, minimum_percent=70)
    core.SETTINGS['linkedin_experience_levels'] = []
    for name in ('linkedin', 'indeed', 'wellfound', 'remoteok', 'himalayas', 'weworkremotely'):
        core.CFG['sources'][name] = name == 'linkedin'
    core.EXTERNAL = True


def contains(term, text):
    return bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.I))


def geography(location):
    """Location label only, not arbitrary company-history prose or citizenship."""
    if not isinstance(location, str) or not location.strip():
        return 'geography_unknown'
    us = bool(re.search(r'(?<!\w)(?:US|U\.S\.|U\.S\.A\.)(?!\w)', location)) or any(
        contains(term, location) for term in ['United States', 'USA'])
    if any(contains(term, location) for term in AMBIGUOUS if term != 'Georgia' or not us):
        return 'geography_ambiguous'
    allowed = us or any(contains(term, location) for term in [*EUROPE, *AMERICAS, *AFRICA, *OCEANIA,
                                                     'Worldwide', 'Global', 'Europe', 'Africa', 'Americas',
                                                     'Latin America', 'Caribbean', 'Oceania', 'United States of America', 'USA', 'UK',
                                                     'LATAM', 'North America', 'South America', 'Central America',
                                                     'European Union', 'European Economic Area', 'EU', 'EEA', 'ANZ',
                                                     'Antarctica', 'South Georgia and the South Sandwich Islands', 'Bouvet Island'])
    excluded = any(contains(term, location) for term in [*ASIA, 'Asia', 'APAC', 'Asia-Pacific'])
    if not allowed and any(contains(term, location) for term in ['EMEA', 'APAC', 'Asia-Pacific', 'Asia Pacific', 'Middle East']):
        return 'geography_ambiguous'
    if excluded and not allowed:
        return 'geography_excluded'
    if allowed:
        return None
    return 'geography_unknown'


def job_geography(job):
    metadata = job.get('metadata') or {}
    location = metadata.get('location', '')
    if not location:
        # Use only scoped current-job headers, never descriptions/recommendations.
        lines = (metadata.get('diagnostic') or {}).get('header_text', '').splitlines()
        locations = [line.strip() for line in lines if len(line.strip()) <= 150
                     and line.strip() not in (job.get('title'), job.get('company'))
                     and geography(line) != 'geography_unknown']
        location = ', '.join(dict.fromkeys(locations))
    return location, geography(location)
