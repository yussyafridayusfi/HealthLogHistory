#!/usr/bin/env python3
"""
jmx_to_postman.py — deterministic JMeter (.jmx) -> Postman importer.

Pipeline:  JMX XML  ->  intermediate model (IR)  ->  Collection v2.1 JSON
                                                  ->  performance run config
                                                  ->  import report

Design: everything structural is deterministic and stdlib-only (xml.etree, json,
argparse). The only judgment calls left to a model are (a) translating
JSR223/BeanShell scripts to JavaScript and (b) resolving ambiguous extractors
(XPath/CSS/complex JSONPath). Those are emitted as explicit `manual_tasks` in the
report so a model can finish them.

Usage:
    python3 jmx_to_postman.py INPUT.jmx -o OUTDIR [--name NAME]

Outputs in OUTDIR:
    <name>.postman_collection.json   Collection v2.1
    <name>.perf.json                 machine-readable perf config
    <name>.perf.md                   human-readable perf config + CLI command
    <name>.report.md                 import report (what/approx/manual)
"""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

try:
    from groovy_to_js import translate as groovy_translate
except ImportError:  # allow running from any cwd
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from groovy_to_js import translate as groovy_translate

SCHEMA_V21 = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

# ResponseAssertion test_type bit flags (org.apache.jmeter.assertions.AssertionGui)
F_MATCH, F_CONTAINS, F_NOT, F_EQUALS, F_SUBSTRING, F_OR = 1, 2, 4, 8, 16, 32

# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _localname(tag):
    return tag.split('}')[-1] if '}' in tag else tag


def get_prop(el, name, default=''):
    """Value of a direct <*Prop name="..."> child, as text."""
    for child in el:
        if child.get('name') == name and _localname(child.tag).endswith('Prop'):
            return (child.text or '').strip()
    return default


def get_bool(el, name, default=False):
    v = get_prop(el, name, '').lower()
    if v == '':
        return default
    return v == 'true'


def get_element_prop(el, name):
    """A nested <elementProp name="..."> child element, or None."""
    for child in el:
        if child.get('name') == name and _localname(child.tag) == 'elementProp':
            return child
    return None


def get_collection_prop(el, name):
    for child in el:
        if child.get('name') == name and _localname(child.tag) == 'collectionProp':
            return child
    return None


def iter_pairs(hashtree):
    """Yield (element, following_hashTree_or_None) for a <hashTree>.

    JMeter serialises children as: element, hashTree, element, hashTree, ...
    where each element's children live in the hashTree that immediately follows
    it. This pairing is the backbone of the whole parser.
    """
    if hashtree is None:
        return
    pending = None
    for child in list(hashtree):
        if _localname(child.tag) == 'hashTree':
            yield (pending, child)
            pending = None
        else:
            if pending is not None:
                yield (pending, None)
            pending = child
    if pending is not None:
        yield (pending, None)


# ---------------------------------------------------------------------------
# Expression import: JMeter ${...} -> Postman {{...}}
# ---------------------------------------------------------------------------

# A few common JMeter functions with Postman dynamic-variable equivalents.
_FUNC_MAP = {
    '__UUID': '{{$guid}}',
    '__time': '{{$timestamp}}',
    '__threadNum': '{{$randomInt}}',  # approximate; no per-VU index var exists
    '__Random': None,   # handled specially (args)
    '__RandomString': None,
    '__counter': None,
}


def convert_expr(text, warnings=None):
    """Translate a JMeter expression string into Postman variable syntax."""
    if not text:
        return text
    out = text

    # ${__Function(args)} -> best effort
    def repl_func(m):
        fname = m.group(1)
        if fname == '__UUID':
            return '{{$guid}}'
        if fname == '__time':
            return '{{$timestamp}}'
        if fname in ('__Random', '__RandomInt'):
            return '{{$randomInt}}'
        if fname == '__RandomString':
            return '{{$randomAlphaNumeric}}'
        if warnings is not None:
            warnings.append(f'JMeter function {fname}() has no exact Postman equivalent; left as a variable reference for manual review')
        return '{{' + fname.lstrip('_') + '}}'

    out = re.sub(r'\$\{(__\w+)\s*\([^}]*\)\}', repl_func, out)
    # plain ${var} -> {{var}}
    out = re.sub(r'\$\{([^}]+)\}', r'{{\1}}', out)
    return out


# ---------------------------------------------------------------------------
# Intermediate model
# ---------------------------------------------------------------------------
# We keep the IR as plain dicts for easy JSON round-tripping. Shapes:
#   plan = {name, variables:{}, thread_groups:[tg], global_mods:{...}, warnings:[], manual_tasks:[]}
#   tg   = {name, load:{...}, items:[request|folder], warnings:[]}
#   request = {name, method, url_parts, headers:[], body:{}, auth, extractors:[],
#              assertions:[], pre_scripts:[], post_scripts:[], timers:[], notes:[]}


SAMPLER_TAGS = {'HTTPSamplerProxy', 'HTTPSampler', 'HTTPSampler2'}
SCRIPT_SAMPLER_TAGS = {'JSR223Sampler', 'BeanShellSampler'}
FOLDER_CONTROLLERS = {'TransactionController', 'GenericController', 'SimpleController'}
FLATTEN_CONTROLLERS = {'LoopController', 'IfController', 'WhileController', 'ForeachController',
                       'OnceOnlyController', 'RuntimeController', 'InterleaveControl',
                       'RandomController', 'RandomOrderController', 'ThroughputController',
                       'CriticalSectionController', 'ModuleController', 'IncludeController'}
TIMER_TAGS = {'ConstantTimer', 'UniformRandomTimer', 'GaussianRandomTimer', 'PoissonRandomTimer',
              'ConstantThroughputTimer', 'SyncTimer', 'BSFTimer', 'JSR223Timer', 'BeanShellTimer'}


def is_thread_group(tag):
    return 'ThreadGroup' in tag


class Ctx:
    """Inherited scope carried down the tree (config elements, header managers,
    auth, scoped assertions/extractors, pre/post scripts, http defaults)."""
    def __init__(self):
        self.headers = []       # list of (name, value)
        self.auth = None        # dict
        self.http_defaults = {} # protocol/domain/port/path/timeouts
        self.assertions = []    # dicts
        self.extractors = []    # dicts
        self.pre_scripts = []   # dicts {lang, script, name}
        self.post_scripts = []
        self.variables = {}     # name -> value (User Defined Variables in scope)

    def child(self):
        c = Ctx()
        c.headers = list(self.headers)
        c.auth = self.auth
        c.http_defaults = dict(self.http_defaults)
        c.assertions = list(self.assertions)
        c.extractors = list(self.extractors)
        c.pre_scripts = list(self.pre_scripts)
        c.post_scripts = list(self.post_scripts)
        c.variables = dict(self.variables)
        return c


# ---------------------------------------------------------------------------
# Element parsers (modifiers)
# ---------------------------------------------------------------------------

def parse_header_manager(el):
    out = []
    coll = get_collection_prop(el, 'HeaderManager.headers')
    if coll is None:
        return out
    for ep in coll:
        if _localname(ep.tag) != 'elementProp':
            continue
        name = get_prop(ep, 'Header.name')
        value = get_prop(ep, 'Header.value')
        if name:
            out.append((name, value))
    return out


def parse_auth_manager(el, warnings):
    coll = get_collection_prop(el, 'AuthManager.auth_list')
    if coll is None:
        return None
    entries = []
    for ep in coll:
        if _localname(ep.tag) != 'elementProp':
            continue
        entries.append({
            'url': get_prop(ep, 'Authorization.url'),
            'username': get_prop(ep, 'Authorization.username'),
            'password': get_prop(ep, 'Authorization.password'),
            'realm': get_prop(ep, 'Authorization.realm'),
            'mechanism': (get_prop(ep, 'Authorization.mechanism') or 'BASIC').upper(),
        })
    if not entries:
        return None
    first = entries[0]
    mech = first['mechanism']
    if len(entries) > 1:
        warnings.append(f'AuthManager has {len(entries)} credential entries; only the first was applied as collection auth. Review per-URL credentials.')
    if mech in ('KERBEROS', 'NTLM'):
        warnings.append(f'Auth mechanism {mech} is not supported by Postman performance runs; emitted as basic auth placeholder.')
        mech = 'BASIC'
    kind = 'digest' if mech == 'DIGEST' else 'basic'
    return {'type': kind, 'username': convert_expr(first['username']), 'password': convert_expr(first['password'])}


def parse_http_defaults(el):
    return {
        'protocol': get_prop(el, 'HTTPSampler.protocol'),
        'domain': get_prop(el, 'HTTPSampler.domain'),
        'port': get_prop(el, 'HTTPSampler.port'),
        'path': get_prop(el, 'HTTPSampler.path'),
        'contentEncoding': get_prop(el, 'HTTPSampler.contentEncoding'),
    }


def parse_user_variables(el):
    """Arguments element -> {name: value}."""
    out = {}
    coll = get_collection_prop(el, 'Arguments.arguments')
    if coll is None:
        return out
    for ep in coll:
        if _localname(ep.tag) != 'elementProp':
            continue
        name = get_prop(ep, 'Argument.name') or ep.get('name')
        value = get_prop(ep, 'Argument.value')
        if name:
            out[name] = value
    return out


def parse_csv_dataset(el, warnings):
    var_names = get_prop(el, 'variableNames')
    return {
        'filename': get_prop(el, 'filename'),
        'variableNames': [v.strip() for v in var_names.split(',') if v.strip()] if var_names else [],
        'delimiter': get_prop(el, 'delimiter', ','),
        'recycle': get_bool(el, 'recycle', True),
        'ignoreFirstLine': get_bool(el, 'ignoreFirstLine', False),
        'quotedData': get_bool(el, 'quotedData', False),
    }


def timer_delay_ms(el, tag):
    """Representative think-time delay (ms) for a timer element, or None."""
    if tag == 'ConstantTimer':
        return _to_int(get_prop(el, 'ConstantTimer.delay'), 0) or None
    if tag in ('UniformRandomTimer', 'GaussianRandomTimer', 'PoissonRandomTimer'):
        # offset is the fixed part; the random part averages out across requests
        return _to_int(get_prop(el, 'ConstantDelayOffset'), 0) or None
    return None


def convert_if_condition(cond):
    """Best-effort JMeter If-Controller condition -> a JS boolean guard.
    ${var} becomes pm.variables.get("var"); function calls are left for review."""
    if not cond or not cond.strip():
        return 'true'
    js = re.sub(r'\$\{(__\w+)\s*\([^}]*\)\}', 'true /* review: JMeter function */', cond)
    # quoted variable "${x}" / '${x}' -> String(pm.variables.get("x")) so the
    # surrounding quotes don't produce nested-quote (syntactically broken) JS.
    js = re.sub(r'''(["'])\$\{([^}{]+)\}\1''', r'String(pm.variables.get("\2"))', js)
    js = re.sub(r'\$\{([^}]+)\}', r'pm.variables.get("\1")', js)
    return js.strip()


def _safe_condition(cond):
    """True if `cond` is confidently valid JS we can inline as a guard. Anything
    with a leftover bare identifier (e.g. a JMeter placeholder like IF1.1) is
    rejected so we never emit a script that fails to parse."""
    t = re.sub(r'/\*.*?\*/', '', cond)                   # review comments
    t = re.sub(r'"[^"]*"', '', t)                        # double-quoted strings
    t = re.sub(r"'[^']*'", '', t)                        # single-quoted strings
    t = re.sub(r'(String\()?pm\.variables\.get\([^)]*\)\)?', '', t)  # var reads
    t = re.sub(r'\b(true|false|null|undefined)\b', '', t)
    t = re.sub(r'\d+(\.\d+)?', '', t)                    # numbers
    t = re.sub(r'[<>=!&|()+\-*/%?:,.\s]', '', t)         # allowed operators/punct
    return t == ''


def parse_script(el, tag):
    """JSR223 / BeanShell element -> {lang, script, raw_name}."""
    if tag.startswith('BeanShell'):
        src = get_prop(el, 'BeanShellSampler.query') or get_prop(el, 'query') or get_prop(el, 'script')
        lang = 'beanshell'
    else:
        src = get_prop(el, 'script')
        lang = get_prop(el, 'scriptLanguage') or 'groovy'
    return {'lang': lang, 'script': src, 'name': el.get('testname', '')}


# --- extractors -------------------------------------------------------------

def parse_extractor(el, tag):
    """Return a normalised extractor dict or None if unrecognised."""
    if tag == 'RegexExtractor':
        return {'kind': 'regex',
                'ref': get_prop(el, 'RegexExtractor.refname'),
                'regex': get_prop(el, 'RegexExtractor.regex'),
                'template': get_prop(el, 'RegexExtractor.template', '$1$'),
                'match_no': get_prop(el, 'RegexExtractor.match_number', '1'),
                'default': get_prop(el, 'RegexExtractor.default'),
                'from_headers': get_bool(el, 'RegexExtractor.useHeaders')}
    if tag == 'BoundaryExtractor':
        return {'kind': 'boundary',
                'ref': get_prop(el, 'BoundaryExtractor.refname'),
                'lboundary': get_prop(el, 'BoundaryExtractor.lboundary'),
                'rboundary': get_prop(el, 'BoundaryExtractor.rboundary'),
                'match_no': get_prop(el, 'BoundaryExtractor.match_number', '1'),
                'default': get_prop(el, 'BoundaryExtractor.default'),
                'from_headers': get_bool(el, 'BoundaryExtractor.useHeaders')}
    if tag == 'JSONPostProcessor':
        refs = get_prop(el, 'JSONPostProcessor.referenceNames')
        exprs = get_prop(el, 'JSONPostProcessor.jsonPathExprs')
        defs = get_prop(el, 'JSONPostProcessor.defaultValues')
        return {'kind': 'jsonpath_multi',
                'refs': [r.strip() for r in refs.split(';')] if refs else [],
                'exprs': [e.strip() for e in exprs.split(';')] if exprs else [],
                'defaults': [d.strip() for d in defs.split(';')] if defs else []}
    if tag.endswith('JSONPathExtractor'):   # atlantbh plugin
        return {'kind': 'jsonpath_single',
                'ref': get_prop(el, 'VAR'),
                'expr': get_prop(el, 'JSONPATH'),
                'default': get_prop(el, 'DEFAULT')}
    if tag in ('XPathExtractor', 'XPath2Extractor'):
        return {'kind': 'xpath',
                'ref': get_prop(el, 'XPathExtractor.refname'),
                'query': get_prop(el, 'XPathExtractor.xpathQuery') or get_prop(el, 'XPathExtractor.xpathQuery'),
                'default': get_prop(el, 'XPathExtractor.default')}
    if tag == 'HtmlExtractor':
        return {'kind': 'css',
                'ref': get_prop(el, 'HtmlExtractor.refname'),
                'expr': get_prop(el, 'HtmlExtractor.expr'),
                'attr': get_prop(el, 'HtmlExtractor.attribute'),
                'default': get_prop(el, 'HtmlExtractor.default')}
    return None


# --- assertions -------------------------------------------------------------

def parse_assertion(el, tag):
    if tag == 'ResponseAssertion':
        strings = []
        coll = get_collection_prop(el, 'Asserion.test_strings')  # yes, JMeter typo
        if coll is None:
            coll = get_collection_prop(el, 'Assertion.test_strings')
        if coll is not None:
            for sp in coll:
                strings.append((sp.text or '').strip())
        try:
            ttype = int(get_prop(el, 'Assertion.test_type', '0') or '0')
        except ValueError:
            ttype = 0
        return {'kind': 'response',
                'field': get_prop(el, 'Assertion.test_field', 'Assertion.response_data'),
                'test_type': ttype,
                'strings': [s for s in strings if s],
                'assume_success': get_bool(el, 'Assertion.assume_success')}
    if tag == 'DurationAssertion':
        return {'kind': 'duration', 'ms': get_prop(el, 'DurationAssertion.duration')}
    if tag == 'SizeAssertion':
        return {'kind': 'size', 'bytes': get_prop(el, 'SizeAssertion.size'),
                'op': get_prop(el, 'SizeAssertion.operator', '1')}
    if tag in ('JSONPathAssertion', 'JMESPathAssertion'):
        return {'kind': 'jsonpath',
                'path': get_prop(el, 'JSON_PATH'),
                'expected': get_prop(el, 'EXPECTED_VALUE'),
                'validate': get_bool(el, 'JSONVALIDATION')}
    return None


# ---------------------------------------------------------------------------
# HTTP sampler -> request IR
# ---------------------------------------------------------------------------

def parse_sampler(el, ctx, warnings):
    name = el.get('testname', 'Request')
    raw_method = get_prop(el, 'HTTPSampler.method') or 'GET'
    # A variable-driven method (${method}) must become {{method}}, not be
    # uppercased into a broken variable name.
    method = convert_expr(raw_method, warnings) if '${' in raw_method else raw_method.upper()
    d = ctx.http_defaults
    path = get_prop(el, 'HTTPSampler.path') or d.get('path') or '/'
    absolute = path.startswith('http://') or path.startswith('https://')
    protocol = get_prop(el, 'HTTPSampler.protocol') or d.get('protocol') or ''
    if not protocol and not absolute:
        protocol = 'http'
        warnings.append('Some requests had no protocol set in JMeter; defaulted to http (JMeter default). Switch to https if the target requires TLS.')
    domain = get_prop(el, 'HTTPSampler.domain') or d.get('domain') or ''
    port = get_prop(el, 'HTTPSampler.port') or d.get('port') or ''
    raw_body = get_bool(el, 'HTTPSampler.postBodyRaw')

    # arguments
    args = []
    argcoll = None
    ep = get_element_prop(el, 'HTTPsampler.Arguments')
    if ep is not None:
        argcoll = get_collection_prop(ep, 'Arguments.arguments')
    if argcoll is not None:
        for a in argcoll:
            if _localname(a.tag) != 'elementProp':
                continue
            args.append({'name': get_prop(a, 'Argument.name'),
                         'value': get_prop(a, 'Argument.value')})

    body = None
    query = []
    if raw_body:
        raw = args[0]['value'] if args else ''
        body = {'mode': 'raw', 'raw': convert_expr(raw)}
    elif method in ('POST', 'PUT', 'PATCH') and args:
        if get_bool(el, 'HTTPSampler.DO_MULTIPART_POST'):
            body = {'mode': 'formdata',
                    'formdata': [{'key': convert_expr(a['name']), 'value': convert_expr(a['value']), 'type': 'text'} for a in args if a['name']]}
        else:
            body = {'mode': 'urlencoded',
                    'urlencoded': [{'key': convert_expr(a['name']), 'value': convert_expr(a['value']), 'type': 'text'} for a in args if a['name']]}
    elif method in ('HEAD', 'TRACE') and args:
        # JMeter does not append sampler parameters as a query string for HEAD/TRACE.
        warnings.append(f'Request "{name}" ({method}): JMeter ignores separate parameters for {method}; they were dropped from the URL.')
    else:
        query = [{'key': convert_expr(a['name']), 'value': convert_expr(a['value'])} for a in args if a['name']]

    # headers: JMeter merges every in-scope HeaderManager. Keep the last value per
    # name (nearer scope / later entry wins) and warn when a duplicate collapsed,
    # rather than silently dropping or emitting two same-named headers.
    hdr = {}
    dup = False
    for n, v in ctx.headers:
        k = n.lower()
        if k in hdr:
            dup = True
        hdr[k] = (n, v)
    headers = [{'key': convert_expr(o), 'value': convert_expr(v)} for (o, v) in hdr.values()]
    if dup:
        warnings.append(f'Request "{name}" had duplicate header names; kept the last value per name (JMeter merges in-scope managers). Verify the intended values.')

    req = {
        'name': name,
        'method': method,
        'url': {
            'protocol': protocol.lower() if protocol else '',
            'domain': convert_expr(domain),
            'port': port,
            'path': convert_expr(path),
            'query': query,
        },
        'headers': headers,
        'body': body,
        'auth': ctx.auth,
        'extractors': list(ctx.extractors),
        'assertions': list(ctx.assertions),
        'pre_scripts': list(ctx.pre_scripts),
        'post_scripts': list(ctx.post_scripts),
        'timers': [],
        'guards': [],
        'notes': [],
    }
    return req


# ---------------------------------------------------------------------------
# Recursive tree walk
# ---------------------------------------------------------------------------

def collect_modifiers(pairs, ctx, plan):
    """First pass at a level: fold config/assertion/extractor/script elements
    into a child context that applies to every sampler at this level and below."""
    c = ctx.child()
    for el, _sub in pairs:
        if el is None:
            continue
        tag = _localname(el.tag)
        if el.get('enabled', 'true') == 'false':
            continue
        if tag == 'HeaderManager':
            for n, v in parse_header_manager(el):
                c.headers.append((n, v))
        elif tag == 'AuthManager':
            a = parse_auth_manager(el, plan['warnings'])
            if a:
                c.auth = a
        elif tag == 'ConfigTestElement' and el.get('guiclass') == 'HttpDefaultsGui':
            c.http_defaults.update({k: v for k, v in parse_http_defaults(el).items() if v})
        elif tag == 'Arguments':
            c.variables.update(parse_user_variables(el))
            plan['variables'].update({k: convert_expr(v) for k, v in parse_user_variables(el).items()})
        elif tag == 'CSVDataSet':
            plan['data_sets'].append(parse_csv_dataset(el, plan['warnings']))
        elif tag in ('JSR223PreProcessor', 'BeanShellPreProcessor'):
            c.pre_scripts.append(parse_script(el, tag))
        elif tag in ('JSR223PostProcessor', 'BeanShellPostProcessor'):
            c.post_scripts.append(parse_script(el, tag))
        elif tag == 'CookieManager':
            plan['warnings'].append('HTTP Cookie Manager found: Postman manages cookies automatically per virtual user; preset cookies were not transferred.')
        elif tag in ('CacheManager', 'DNSCacheManager'):
            plan['warnings'].append(f'{tag} is a no-op in Postman and was skipped.')
        else:
            ext = parse_extractor(el, tag)
            if ext:
                c.extractors.append(ext)
                continue
            asrt = parse_assertion(el, tag)
            if asrt:
                c.assertions.append(asrt)
    return c


def _attach_skip_guard(entry, cond_js):
    """Recursively add an If-Controller skip guard to every request in an entry."""
    if entry['type'] == 'request':
        entry['req'].setdefault('guards', []).append(cond_js)
    elif entry['type'] == 'folder':
        for child in entry['items']:
            _attach_skip_guard(child, cond_js)


def walk(hashtree, ctx, plan, out_items):
    """Walk a hashTree, appending request/folder IR dicts into out_items."""
    pairs = list(iter_pairs(hashtree))
    c = collect_modifiers(pairs, ctx, plan)

    for el, sub in pairs:
        if el is None:
            continue
        tag = _localname(el.tag)
        if el.get('enabled', 'true') == 'false':
            continue

        if tag in SAMPLER_TAGS:
            # Collect the sampler's own hashTree first so request-scoped header
            # managers, auth, HTTP defaults, extractors and assertions all apply.
            sc = collect_modifiers(list(iter_pairs(sub)), c, plan) if sub is not None else c
            req = parse_sampler(el, sc, plan['warnings'])
            req['extractors'] = sc.extractors
            req['assertions'] = sc.assertions
            req['post_scripts'] = sc.post_scripts
            req['pre_scripts'] = sc.pre_scripts
            if sub is not None:
                # timers under the sampler = think time, which Postman performance
                # runs have no equivalent for. Report it; never fake it.
                for tel, _ in iter_pairs(sub):
                    if tel is not None and _localname(tel.tag) in TIMER_TAGS:
                        ttag = _localname(tel.tag)
                        req['timers'].append(ttag)
                        d = timer_delay_ms(tel, ttag)
                        suffix = f' ({d}ms)' if d else ''
                        plan['warnings'].append(f'Timer "{tel.get("testname", ttag)}"{suffix} on request "{req["name"]}" is think time; Postman performance runs have no think time, so it was dropped.')
            out_items.append({'type': 'request', 'req': req})

        elif tag in SCRIPT_SAMPLER_TAGS:
            sc = parse_script(el, tag)
            plan['script_samplers'].append(sc)
            plan['warnings'].append(f'Script sampler "{sc["name"]}" ({sc["lang"]}) makes no HTTP call; emitted as a script-only request stub for manual review.')
            out_items.append({'type': 'script_request', 'script': sc})

        elif tag in FOLDER_CONTROLLERS:
            folder = {'type': 'folder', 'name': el.get('testname', 'Group'), 'items': []}
            walk(sub, c, plan, folder['items'])
            if folder['items']:
                out_items.append(folder)

        elif tag == 'IfController':
            # Import the condition into a per-request skip guard rather than
            # running the enclosed requests unconditionally.
            cond_js = convert_if_condition(get_prop(el, 'IfController.condition'))
            inner = []
            walk(sub, c, plan, inner)
            for entry in inner:
                _attach_skip_guard(entry, cond_js)
            out_items.extend(inner)
            plan['warnings'].append(f'If Controller "{el.get("testname","")}" -> a pre-request skip guard ({cond_js}) was added to each enclosed request (pm.execution.skipRequest). Review the condition.')

        elif tag in FLATTEN_CONTROLLERS:
            # flatten children into the current level
            walk(sub, c, plan, out_items)

        elif tag in TIMER_TAGS:
            d = timer_delay_ms(el, tag)
            suffix = f', {d}ms' if d else ''
            plan['warnings'].append(f'Timer "{el.get("testname", tag)}" ({tag}{suffix}) at group/plan scope is think time / pacing; Postman performance runs have no think time, so it was dropped.')

        elif tag == 'TestAction':
            plan['warnings'].append(f'Test Action "{el.get("testname","")}" (pause/stop) has no Postman performance equivalent and was dropped.')

        elif tag.endswith('Sampler'):
            friendly = tag.split('.')[-1]
            plan['warnings'].append(f'Sampler "{el.get("testname", friendly)}" is a {friendly} (non-HTTP). Postman performance runs execute HTTP requests only; it was skipped.')

        elif is_thread_group(tag):
            # nested thread groups shouldn't appear here; ignore
            pass

        else:
            # Unknown container (RecordingController, TestFragment, custom controller...).
            # Recurse so nested HTTP samplers / timers are not silently lost.
            if sub is not None and len(list(sub)) > 0 and tag not in ('WorkBench', 'ResultCollector'):
                walk(sub, c, plan, out_items)


# ---------------------------------------------------------------------------
# Thread group -> load config
# ---------------------------------------------------------------------------

def parse_thread_group(el):
    """Normalise any thread-group variant into a common load dict."""
    tag = _localname(el.tag)
    name = el.get('testname', 'Thread Group')

    if 'ConcurrencyThreadGroup' in tag or 'ArrivalsThreadGroup' in tag:
        unit = get_prop(el, 'Unit', 'S')
        ramp = _to_int(get_prop(el, 'RampUp'))
        hold = _to_int(get_prop(el, 'Hold'))
        target = _to_int(get_prop(el, 'TargetLevel'), 1)
        mult = 60 if unit.upper().startswith('M') else 1
        dur = (ramp + hold) * mult
        return {'name': name, 'kind': 'concurrency', 'vus': target,
                'ramp_s': ramp * mult, 'duration_s': dur, 'loops': None,
                'scheduler': True, 'raw': {'TargetLevel': target, 'RampUp': ramp, 'Hold': hold, 'Steps': get_prop(el, 'Steps'), 'Unit': unit}}

    if 'SteppingThreadGroup' in tag:
        threads = _to_int(get_prop(el, 'ThreadGroup.num_threads'), 1)
        return {'name': name, 'kind': 'stepping', 'vus': threads,
                'ramp_s': _to_int(get_prop(el, 'rampUp')), 'duration_s': _to_int(get_prop(el, 'flighttime')),
                'loops': None, 'scheduler': True,
                'raw': {'num_threads': threads, 'Start users count': get_prop(el, 'Start users count'),
                        'Start users period': get_prop(el, 'Start users period'), 'flighttime': get_prop(el, 'flighttime')}}

    if 'UltimateThreadGroup' in tag:
        # collectionProp of rows [start, startup, hold, shutdown]; take max concurrency & total time
        maxv, total = 1, 0
        coll = get_collection_prop(el, 'ultimatethreadgroupdata')
        if coll is not None:
            for row in coll:
                vals = [(_localname(c.tag), (c.text or '').strip()) for c in row]
                nums = [_to_int(v) for _, v in vals]
                if nums:
                    maxv = max(maxv, nums[0] if nums else 1)
                    total = max(total, sum(nums[1:]) if len(nums) > 1 else 0)
        return {'name': name, 'kind': 'ultimate', 'vus': maxv, 'ramp_s': 0,
                'duration_s': total, 'loops': None, 'scheduler': True, 'raw': {'rows': 'see jmx'}}

    # standard ThreadGroup
    threads = _to_int(get_prop(el, 'ThreadGroup.num_threads'), 1)
    ramp = _to_int(get_prop(el, 'ThreadGroup.ramp_time'))
    scheduler = get_bool(el, 'ThreadGroup.scheduler')
    duration = _to_int(get_prop(el, 'ThreadGroup.duration')) if scheduler else 0
    loops = None
    mc = get_element_prop(el, 'ThreadGroup.main_controller')
    if mc is not None:
        if get_bool(mc, 'LoopController.continue_forever'):
            loops = -1
        else:
            loops = _to_int(get_prop(mc, 'LoopController.loops'), 1)
    return {'name': name, 'kind': 'standard', 'vus': threads, 'ramp_s': ramp,
            'duration_s': duration, 'loops': loops, 'scheduler': scheduler, 'raw': {}}


def _to_int(s, default=0):
    try:
        return int(float(str(s).strip()))
    except (ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Top-level parse
# ---------------------------------------------------------------------------

_MAX_JMX_BYTES = 25 * 1024 * 1024


class _NoDTDBuilder(ET.TreeBuilder):
    """Refuse any DOCTYPE so a crafted JMX can't pull an XXE / entity bomb."""
    def doctype(self, name, pubid, system):
        raise ValueError('DTD/DOCTYPE is not allowed in a JMX file (XXE safety)')


def _secure_parse(path):
    with open(path, 'rb') as f:
        data = f.read(_MAX_JMX_BYTES + 1)
    if len(data) > _MAX_JMX_BYTES:
        raise ValueError('JMX file exceeds the 25 MiB safety cap')
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise ValueError('JMX contains a DTD/ENTITY declaration; refused for XXE safety')
    return ET.fromstring(data, parser=ET.XMLParser(target=_NoDTDBuilder()))


def parse_jmx(path):
    root = _secure_parse(path)
    top = None
    for child in root:
        if _localname(child.tag) == 'hashTree':
            top = child
            break
    if top is None:
        raise ValueError('No <hashTree> under <jmeterTestPlan>; not a valid JMX file.')

    plan = {'name': None, 'variables': {}, 'thread_groups': [], 'data_sets': [],
            'warnings': [], 'script_samplers': [], 'global_mods': None,
            'request_delay_ms': None}

    # Locate the TestPlan + its hashTree
    testplan_el, testplan_ht = None, None
    for el, sub in iter_pairs(top):
        if el is not None and _localname(el.tag) == 'TestPlan':
            testplan_el, testplan_ht = el, sub
            break
    if testplan_el is None:
        # some plans put thread groups directly under top (rare)
        testplan_ht = top
        plan['name'] = 'Imported Test Plan'
    else:
        plan['name'] = testplan_el.get('testname', 'Imported Test Plan')
        udv = get_element_prop(testplan_el, 'TestPlan.user_defined_variables')
        if udv is not None:
            plan['variables'].update({k: convert_expr(v) for k, v in parse_user_variables(udv).items()})

    # Plan-level modifiers (apply to all thread groups)
    plan_pairs = list(iter_pairs(testplan_ht))
    base_ctx = collect_modifiers(plan_pairs, Ctx(), plan)

    # Thread groups
    for el, sub in plan_pairs:
        if el is None:
            continue
        tag = _localname(el.tag)
        if is_thread_group(tag):
            if el.get('enabled', 'true') == 'false':
                plan['warnings'].append(f'Thread group "{el.get("testname","")}" is disabled; skipped.')
                continue
            load = parse_thread_group(el)
            for prop_name in ('ThreadGroup.num_threads', 'ThreadGroup.ramp_time', 'ThreadGroup.duration', 'TargetLevel', 'RampUp', 'Hold'):
                raw = get_prop(el, prop_name)
                if raw and '${' in raw:
                    plan['warnings'].append(f'Thread group "{load["name"]}" has a variable-driven load value {prop_name}="{raw}" that could not be resolved to a number; set VUs/duration manually.')
            # setUp / tearDown thread groups run once per lifecycle, not looped by
            # every VU. Tag them so they are severed into their own collections.
            if 'SetupThreadGroup' in tag:
                load['role'] = 'setup'
            elif 'PostThreadGroup' in tag:
                load['role'] = 'teardown'
            else:
                load['role'] = 'main'
            items = []
            walk(sub, base_ctx, plan, items)
            plan['thread_groups'].append({'load': load, 'items': items})

    if not plan['thread_groups']:
        # Capture samplers/scripts that live outside any thread group (root-level
        # JSR223/BeanShell samplers, plans with no thread group wrapper).
        items = []
        walk(testplan_ht, base_ctx, plan, items)
        if items:
            default_load = {'name': plan['name'], 'kind': 'standard', 'vus': 1, 'ramp_s': 0,
                            'duration_s': 0, 'loops': 1, 'scheduler': False, 'raw': {}}
            plan['thread_groups'].append({'load': default_load, 'items': items})
            plan['warnings'].append('No thread group wraps these requests; assumed a default 1-VU load. Set your own VU count and duration.')

    if not plan['thread_groups']:
        plan['warnings'].append('No enabled thread groups (or importable HTTP samplers) found; the collection is empty.')

    # dedupe warnings (preserve order) and data sets (by filename)
    seen, dd = set(), []
    for w in plan['warnings']:
        if w not in seen:
            seen.add(w)
            dd.append(w)
    plan['warnings'] = dd
    seenf, dsd = set(), []
    for ds in plan['data_sets']:
        if ds['filename'] not in seenf:
            seenf.add(ds['filename'])
            dsd.append(ds)
    plan['data_sets'] = dsd
    return plan


# ===========================================================================
# BUILDERS
# ===========================================================================

def _escape_js(s):
    return (s or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')


def _wrap_iife(lines):
    """Wrap a translated script body so a top-level `return` (common in
    BeanShell/JSR223) is legal JavaScript and local decls don't leak."""
    body = [('    ' + ln) if ln else ln for ln in lines]
    return ['(function () {'] + body + ['})();']


def extractor_to_js(ext, manual_tasks, req_name):
    """Return list of JS lines for one extractor (post-response)."""
    lines = []
    k = ext['kind']
    if k == 'regex':
        ref = ext['ref'] or 'extracted'
        if not ext['regex']:
            return [f'// [skipped] empty regex extractor "{ref}"']
        grp = re.findall(r'\$(\d+)\$', ext.get('template', '$1$'))
        gi = grp[0] if grp else '1'
        src = 'pm.response.headers.toString()' if ext.get('from_headers') else 'pm.response.text()'
        pat = _escape_js(ext['regex'])
        dflt = _escape_js(ext.get('default', ''))
        ref_e = _escape_js(ref)
        lines.append(f'try {{ var _m = String({src}).match(new RegExp("{pat}")); pm.collectionVariables.set("{ref_e}", _m ? _m[{gi}] : "{dflt}"); }} catch (e) {{ pm.collectionVariables.set("{ref_e}", "{dflt}"); }}')
    elif k == 'boundary':
        ref = ext['ref'] or 'extracted'
        lb = re.escape(ext['lboundary']) if ext['lboundary'] else ''
        rb = re.escape(ext['rboundary']) if ext['rboundary'] else ''
        if not lb and not rb:
            return [f'// [skipped] boundary extractor "{ref}" has no boundaries']
        src = 'pm.response.headers.toString()' if ext.get('from_headers') else 'pm.response.text()'
        pat = _escape_js(lb + '([\\s\\S]*?)' + rb)
        dflt = _escape_js(ext.get('default', ''))
        ref_e = _escape_js(ref)
        lines.append(f'try {{ var _b = String({src}).match(new RegExp("{pat}")); pm.collectionVariables.set("{ref_e}", _b ? _b[1] : "{dflt}"); }} catch (e) {{ pm.collectionVariables.set("{ref_e}", "{dflt}"); }}')
    elif k == 'jsonpath_multi':
        for ref, expr, dflt in zip(ext['refs'], ext['exprs'], ext['defaults'] + [''] * len(ext['refs'])):
            access = _jsonpath_to_js(expr, manual_tasks, req_name, ref)
            if access is None:
                lines.append(f'// [manual] JSONPath "{expr}" -> set "{ref}"; too complex for auto-import')
            else:
                lines.append(f'try {{ pm.collectionVariables.set("{_escape_js(ref)}", {access}); }} catch (e) {{ pm.collectionVariables.set("{_escape_js(ref)}", "{_escape_js(dflt)}"); }}')
    elif k == 'jsonpath_single':
        ref = ext['ref'] or 'extracted'
        access = _jsonpath_to_js(ext['expr'], manual_tasks, req_name, ref)
        if access is None:
            lines.append(f'// [manual] JSONPath "{ext["expr"]}" -> set "{ref}"; too complex for auto-import')
        else:
            lines.append(f'try {{ pm.collectionVariables.set("{_escape_js(ref)}", {access}); }} catch (e) {{ pm.collectionVariables.set("{_escape_js(ref)}", "{_escape_js(ext.get("default",""))}"); }}')
    elif k in ('xpath', 'css'):
        ref = ext.get('ref') or 'extracted'
        manual_tasks.append({'request': req_name, 'kind': k,
                             'detail': f'{k.upper()} extractor "{ref}" ({ext.get("query") or ext.get("expr")}) needs manual import; Postman sandbox has no built-in {k.upper()} support.'})
        lines.append(f'// [manual] {k.upper()} extractor "{ref}" — see import report')
    return lines


def _jsonpath_to_js(expr, manual_tasks, req_name, ref):
    """Import simple JSONPath ($.a.b[0].c) into a JS accessor on the parsed body.
    Returns None for anything with filters/wildcards/recursion."""
    if not expr:
        return None
    e = expr.strip()
    if e in ('$', '$.'):
        return 'pm.response.json()'
    if not e.startswith('$'):
        return None
    if any(tok in e for tok in ['*', '..', '?', '(', ')', '@', ',', ':']):
        return None
    body = e[1:]
    if body.startswith('.'):
        body = body[1:]
    # split into dot/bracket segments
    js = 'pm.response.json()'
    for seg in re.split(r'\.(?![^\[]*\])', body):
        if not seg:
            continue
        m = re.match(r'^(\w+)((\[\d+\])*)$', seg)
        if not m:
            return None
        js += '.' + m.group(1)
        if m.group(2):
            js += m.group(2)
    return js


def assertion_to_js(asrt, idx, req_name):
    """Return (js_lines, pass_if_hint_or_None)."""
    k = asrt['kind']
    if k == 'response':
        return _response_assertion_js(asrt, idx), None
    if k == 'duration':
        ms = (asrt.get('ms') or '').strip()
        if ms.isdigit():
            js = [f'pm.test("Response time < {ms}ms", function () {{', f'    pm.expect(pm.response.responseTime).to.be.below({ms});', '});']
            hint = {'metric': 'p95', 'op': 'less_than', 'value': ms}
        else:
            # a variable-driven threshold (e.g. ${max_ms}) must not be emitted into JS
            js = [f'// [manual] Duration Assertion threshold "{ms}" is not a literal number; add a numeric pm.response.responseTime check by hand.']
            hint = None
        return js, hint
    if k == 'size':
        return [f'pm.test("Response size check", function () {{',
                f'    pm.expect(pm.response.responseSize).to.be.above(0); // JMeter SizeAssertion {asrt.get("op")} {asrt.get("bytes")}', '});'], None
    if k == 'jsonpath':
        path = asrt.get('path', '')
        access = _jsonpath_to_js(path, [], req_name, 'x')
        if access:
            exp = asrt.get('expected')
            if exp:
                return [f'pm.test("JSONPath {path} == {exp}", function () {{',
                        f'    pm.expect(String({access})).to.eql("{_escape_js(exp)}");', '});'], None
            return [f'pm.test("JSONPath {path} exists", function () {{', f'    pm.expect({access}).to.not.be.undefined;', '});'], None
        return [f'// [manual] JSONPath assertion "{path}" needs manual import'], None
    return [], None


def _response_assertion_js(a, idx):
    t = a['test_type']
    field = a['field']
    negate = bool(t & F_NOT)
    if field == 'Assertion.response_code':
        getter = 'pm.response.code'
        is_code = True
    elif field == 'Assertion.response_message':
        getter, is_code = 'pm.response.status', False
    elif field == 'Assertion.response_headers':
        getter, is_code = 'pm.response.headers.toString()', False
    else:
        getter, is_code = 'pm.response.text()', False

    lines = []
    for s in a['strings']:
        label = _escape_js(s)[:40]
        if t & F_EQUALS:
            if is_code:
                expr = f'pm.expect(pm.response.code){".not" if negate else ""}.to.eql({_to_int(s)})'
            else:
                expr = f'pm.expect({getter}){".not" if negate else ""}.to.eql("{_escape_js(s)}")'
            name = f'status is {s}' if is_code else f'equals {label}'
        elif t & F_MATCH or t & F_CONTAINS:
            expr = f'pm.expect(String({getter})){".not" if negate else ""}.to.match(new RegExp("{_escape_js(s)}"))'
            name = f'matches /{label}/'
        else:  # SUBSTRING (16) or default -> plain contains
            val = f'String({getter})' if is_code else getter
            expr = f'pm.expect({val}){".not" if negate else ""}.to.include("{_escape_js(s)}")'
            name = f'contains {label}'
        lines.append(f'pm.test("{name}", function () {{')
        lines.append(f'    {expr};')
        lines.append('});')
    return lines


def build_request_scripts(req, manual_tasks):
    """Assemble prerequest + test event scripts for one request."""
    pre_lines, test_lines = [], []
    pass_if_hints = []

    # If-Controller guards run first: skip the request when the condition is false.
    # Only inline conditions we can prove are valid JS; otherwise comment + flag,
    # so a JMeter placeholder/expression never breaks the whole script.
    for cond in req.get('guards', []):
        if _safe_condition(cond):
            pre_lines.append('// If Controller guard (imported from JMeter) — review condition')
            pre_lines.append(f'if (!({cond})) {{ pm.execution.skipRequest(); }}')
        else:
            pre_lines.append(f'// [manual] If Controller condition could not be safely imported: {cond}')
            pre_lines.append('// Guard this request with pm.execution.skipRequest() once the condition is rewritten.')
            manual_tasks.append({'request': req['name'], 'kind': 'if_condition',
                                 'detail': f'If Controller condition "{cond}" is not valid JS; add a pm.execution.skipRequest() guard manually.'})

    for sc in req.get('pre_scripts', []):
        tr = groovy_translate(sc['script'], sc.get('lang', 'groovy'))
        pre_lines.append(f'// --- imported from JMeter {sc.get("lang","")} pre-processor "{sc.get("name","")}" (REVIEW) ---')
        pre_lines.extend(_wrap_iife(tr['js'].splitlines()))
        manual_tasks.append({'request': req['name'], 'kind': 'script',
                             'detail': f'Pre-processor script ({sc.get("lang")}) auto-translated; review: ' + '; '.join(tr['reasons']) if tr['reasons'] else f'Pre-processor script ({sc.get("lang")}) auto-translated; review for fidelity.'})

    # JMeter fails a sample on HTTP >= 400 even with no assertion. Reproduce that
    # default success check unless the plan already checks the status code or
    # explicitly ignores status (assume_success / "Ignore Status").
    asserts = req.get('assertions', [])
    has_status = any(a.get('kind') == 'response' and a.get('field') == 'Assertion.response_code' for a in asserts)
    ignore_status = any(a.get('kind') == 'response' and a.get('assume_success') for a in asserts)
    if not has_status and not ignore_status:
        test_lines.append("pm.test('JMeter default success (HTTP 200-399)', function () {")
        test_lines.append('    pm.expect(pm.response.code).to.be.within(200, 399);')
        test_lines.append('});')

    for ext in req.get('extractors', []):
        test_lines.extend(extractor_to_js(ext, manual_tasks, req['name']))

    for i, asrt in enumerate(req.get('assertions', [])):
        js, hint = assertion_to_js(asrt, i, req['name'])
        test_lines.extend(js)
        if hint:
            pass_if_hints.append(hint)

    for sc in req.get('post_scripts', []):
        tr = groovy_translate(sc['script'], sc.get('lang', 'groovy'))
        test_lines.append(f'// --- imported from JMeter {sc.get("lang","")} post-processor "{sc.get("name","")}" (REVIEW) ---')
        test_lines.extend(_wrap_iife(tr['js'].splitlines()))
        manual_tasks.append({'request': req['name'], 'kind': 'script',
                             'detail': f'Post-processor script ({sc.get("lang")}) auto-translated; review for fidelity.'})

    events = []
    if pre_lines:
        events.append({'listen': 'prerequest', 'script': {'type': 'text/javascript', 'exec': pre_lines}})
    if test_lines:
        events.append({'listen': 'test', 'script': {'type': 'text/javascript', 'exec': test_lines}})
    return events, pass_if_hints


def build_url(u):
    # JMeter treats port 0 / -1 as "use the protocol default" -> omit it.
    port = u['port'] if u['port'] not in ('', '0', '-1') else ''
    path = u['path'] or ''
    # The path field may itself be a full URL; use it verbatim and ignore host/port.
    if path.startswith('http://') or path.startswith('https://'):
        raw = path
        if u['query']:
            raw += ('&' if '?' in raw else '?') + '&'.join(f'{q["key"]}={q["value"]}' for q in u['query'])
        url = {'raw': raw}
        if u['query']:
            url['query'] = [{'key': q['key'], 'value': q['value']} for q in u['query']]
        return url
    host = u['domain'].split('.') if u['domain'] else []
    raw_scheme = (u['protocol'] + '://') if u['protocol'] else ''
    raw_host = u['domain']
    raw_port = (':' + port) if port else ''
    raw_path = path if path.startswith('/') or not raw_host else '/' + path
    raw = f'{raw_scheme}{raw_host}{raw_port}{raw_path}'
    if u['query']:
        raw += '?' + '&'.join(f'{q["key"]}={q["value"]}' for q in u['query'])
    url = {'raw': raw}
    if u['protocol']:
        url['protocol'] = u['protocol']
    if host:
        url['host'] = host
    if port:
        url['port'] = port
    seg = [p for p in path.split('/') if p != '']
    if seg:
        url['path'] = seg
    if u['query']:
        url['query'] = [{'key': q['key'], 'value': q['value']} for q in u['query']]
    return url


def build_auth(auth):
    if not auth:
        return None
    if auth['type'] == 'basic':
        return {'type': 'basic', 'basic': [{'key': 'username', 'value': auth['username'], 'type': 'string'},
                                           {'key': 'password', 'value': auth['password'], 'type': 'string'}]}
    if auth['type'] == 'digest':
        return {'type': 'digest', 'digest': [{'key': 'username', 'value': auth['username']},
                                             {'key': 'password', 'value': auth['password']}]}
    return None


def build_item(entry, manual_tasks, pass_if_hints):
    if entry['type'] == 'folder':
        children = [build_item(c, manual_tasks, pass_if_hints) for c in entry['items']]
        return {'name': entry['name'], 'item': children}
    if entry['type'] == 'script_request':
        sc = entry['script']
        tr = groovy_translate(sc['script'], sc.get('lang', 'groovy'))
        manual_tasks.append({'request': sc['name'], 'kind': 'script_sampler',
                             'detail': f'Script sampler ({sc.get("lang")}) has no HTTP call. Replace this stub with a real request or move logic into a pre/post script.'})
        stub = [f'// --- JMeter {sc.get("lang","")} script sampler "{sc.get("name","")}" — no HTTP call (REVIEW) ---'] + _wrap_iife(tr['js'].splitlines())
        return {'name': sc['name'] or 'Script Step',
                'event': [{'listen': 'prerequest', 'script': {'type': 'text/javascript', 'exec': stub}}],
                'request': {'method': 'GET', 'header': [], 'url': {'raw': ''}}}
    # request
    req = entry['req']
    events, hints = build_request_scripts(req, manual_tasks)
    pass_if_hints.extend(hints)
    item = {'name': req['name']}
    if events:
        item['event'] = events
    request = {'method': req['method'],
               'header': [{'key': h['key'], 'value': h['value']} for h in req['headers']],
               'url': build_url(req['url'])}
    if req['body']:
        request['body'] = req['body']
    auth = build_auth(req['auth'])
    if auth:
        request['auth'] = auth
    item['request'] = request
    if req.get('timers'):
        item['description'] = 'NOTE: JMeter think-time timers (' + ', '.join(req['timers']) + ') were dropped — Postman performance runs have no think time.'
    return item


def _assemble_items(tgs, manual_tasks, pass_if_hints):
    """Build the Postman item array from a list of thread groups.
    One thread group -> its requests inline; several -> one folder each."""
    items = []
    if len(tgs) == 1:
        for entry in tgs[0]['items']:
            items.append(build_item(entry, manual_tasks, pass_if_hints))
    else:
        for tg in tgs:
            folder = {'name': tg['load']['name'], 'item': []}
            for entry in tg['items']:
                folder['item'].append(build_item(entry, manual_tasks, pass_if_hints))
            items.append(folder)
    return items


def _make_collection(name, items, variables):
    coll = {
        'info': {'name': name, 'schema': SCHEMA_V21, '_postman_id': _stable_id(name)},
        'item': items,
    }
    if variables:
        coll['variable'] = [{'key': k, 'value': v} for k, v in variables.items()]
    return coll


def build_collection(plan, manual_tasks):
    """Return (main_collection, pass_if_hints, extras).

    setUp/tearDown thread groups are severed into their own collections so a
    performance run never loops them per virtual user; they are returned in
    `extras` keyed 'setup'/'teardown' as {'collection': <dict>, 'requests': [...]}.
    """
    pass_if_hints = []
    by_role = {'main': [], 'setup': [], 'teardown': []}
    for tg in plan['thread_groups']:
        by_role.get(tg['load'].get('role', 'main'), by_role['main']).append(tg)
    # Nothing explicitly 'main' (e.g. a plan that is only setUp groups): treat all as main.
    main_tgs = by_role['main'] or plan['thread_groups']

    items = _assemble_items(main_tgs, manual_tasks, pass_if_hints)
    coll = _make_collection(plan['name'], items, plan['variables'])

    extras = {}
    for role in ('setup', 'teardown'):
        if by_role[role] and by_role['main']:
            sub_items = _assemble_items(by_role[role], manual_tasks, [])
            suffix = ' (Setup)' if role == 'setup' else ' (Teardown)'
            sub_coll = _make_collection(plan['name'] + suffix, sub_items, plan['variables'])
            extras[role] = {'collection': sub_coll,
                            'requests': [it['name'] for it in walk_item_names(sub_items)]}
    return coll, pass_if_hints, extras


def walk_item_names(items):
    for it in items:
        if 'item' in it:
            yield from walk_item_names(it['item'])
        else:
            yield it


def _stable_id(seed):
    import hashlib
    h = hashlib.md5((seed or 'jmx').encode()).hexdigest()
    return f'{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}'


# ===========================================================================
# PERF CONFIG
# ===========================================================================

def build_perf_config(plan, pass_if_hints, extras=None, base=None):
    # A performance run loads the main thread groups only; setUp/tearDown run
    # once per lifecycle via separate collections (see `extras`).
    tgs = [tg for tg in plan['thread_groups'] if tg['load'].get('role', 'main') == 'main']
    if not tgs:
        tgs = plan['thread_groups']
    runs = []
    for tg in tgs:
        load = tg['load']
        vus = max(load['vus'], 1)
        # duration: prefer scheduler duration; else approximate from loops
        if load['duration_s']:
            dur_min = max(1, round(load['duration_s'] / 60))
            dur_note = f'from JMeter scheduler duration {load["duration_s"]}s'
        else:
            dur_min = 1
            if load['loops'] == -1:
                dur_note = 'JMeter looped forever; defaulted to 1 min — set your own duration'
            elif load['loops'] and load['loops'] > 0:
                dur_note = f'JMeter ran {load["loops"]} loop(s) (iteration count); Postman runs by time, defaulted to 1 min'
            else:
                dur_note = 'no duration in JMeter; defaulted to 1 min'
        # profile
        if load['kind'] in ('concurrency', 'stepping', 'ultimate') or load['ramp_s'] > 0:
            profile = 'ramp-up'
        else:
            profile = 'fixed'
        runs.append({'name': load['name'], 'vus': vus, 'duration_min': dur_min,
                     'load_profile': profile, 'duration_note': dur_note,
                     'source_kind': load['kind']})

    # pass-if: pick the single tightest response-time hint if any; else a safe default
    pass_if = None
    rt_hints = [h for h in pass_if_hints if h['metric'] in ('p95', 'avg')]
    if rt_hints:
        tightest = min(rt_hints, key=lambda h: _to_int(h['value'], 1 << 30))
        pass_if = f'{tightest["op"]}({tightest["metric"]}, {tightest["value"]})'
    cfg = {
        'runs': runs,
        'pass_if': pass_if,
        'pass_if_suggested': pass_if or 'less_than(error_rate, 5)',
        'data_files': [{'filename': ds['filename'], 'columns': ds['variableNames']} for ds in plan['data_sets']],
        'notes': [],
    }
    if len(runs) > 1:
        cfg['notes'].append('JMeter had multiple thread groups. Postman performance runs execute ONE collection repeated across VUs; they cannot run multiple thread groups concurrently. Each thread group is listed as a separate suggested run — run them individually, or merge into one collection and pick a combined VU/duration.')
    if len(plan['data_sets']) > 1:
        cfg['notes'].append('Multiple CSV Data Sets found. A performance run takes a single --data-file (or one --dataset-id). Merge the CSVs or choose the primary one.')

    # setUp / tearDown collections run once per lifecycle, passed to the runner
    # as --setup / --teardown collections rather than looped per VU.
    extras = extras or {}
    for role in ('setup', 'teardown'):
        if role in extras:
            fname = f'{base}.{role}.postman_collection.json' if base else f'{role}.postman_collection.json'
            cfg[f'{role}_run'] = {'collection': fname, 'requests': extras[role]['requests']}
            cfg['notes'].append(
                f'{role.capitalize()} thread group severed into {fname}; it runs ONCE per run '
                f'(not looped by every virtual user). Pass it to the runner as the {role} collection.')
    return cfg


def render_perf_md(cfg, collection_filename):
    lines = ['# Performance Test Configuration', '']
    for i, run in enumerate(cfg['runs'], 1):
        lines.append(f'## Run {i}: {run["name"]}')
        lines.append('')
        lines.append(f'- Virtual users: **{run["vus"]}**')
        lines.append(f'- Duration: **{run["duration_min"]} min** ({run["duration_note"]})')
        lines.append(f'- Load profile: **{run["load_profile"]}** (from JMeter {run["source_kind"]} thread group)')
        lines.append('')
        cmd = f'postman performance run <COLLECTION_UID> \\\n    --vu-count {run["vus"]} \\\n    --duration {run["duration_min"]} \\\n    --load-profile {run["load_profile"]}'
        if cfg['data_files']:
            cmd += f' \\\n    --data-file "{cfg["data_files"][0]["filename"]}"'
        if cfg['pass_if']:
            cmd += f' \\\n    --pass-if "{cfg["pass_if"]}"'
        else:
            cmd += f' \\\n    --pass-if "{cfg["pass_if_suggested"]}"   # suggested default — no JMeter threshold found'
        lines.append('```bash')
        lines.append(cmd)
        lines.append('```')
        lines.append('')
    if cfg['data_files']:
        lines.append('## Data files')
        for df in cfg['data_files']:
            lines.append(f'- `{df["filename"]}` — columns: {", ".join(df["columns"]) or "(unknown)"}')
        lines.append('')
    if cfg['notes']:
        lines.append('## Notes')
        for n in cfg['notes']:
            lines.append(f'- {n}')
        lines.append('')
    return '\n'.join(lines)


# ===========================================================================
# REPORT
# ===========================================================================

def count_items(items):
    reqs = folders = scripts = 0
    for it in items:
        if it['type'] == 'folder':
            r, f, s = count_items(it['items'])
            folders += 1 + f
            reqs += r
            scripts += s
        elif it['type'] == 'script_request':
            scripts += 1
        else:
            reqs += 1
    return reqs, folders, scripts


def render_report(plan, cfg, manual_tasks, collection_name):
    total_reqs = total_folders = total_scripts = 0
    for tg in plan['thread_groups']:
        r, f, s = count_items(tg['items'])
        total_reqs += r
        total_folders += f
        total_scripts += s

    lines = [f'# Import Report — {plan["name"]}', '']
    lines.append('## Summary')
    lines.append('')
    lines.append(f'- Thread groups: {len(plan["thread_groups"])}')
    lines.append(f'- HTTP requests imported: {total_reqs}')
    lines.append(f'- Folders: {total_folders}')
    lines.append(f'- Script-only samplers (stubs): {total_scripts}')
    lines.append(f'- Collection variables: {len(plan["variables"])}')
    lines.append(f'- Data files: {len(plan["data_sets"])}')
    lines.append(f'- Items needing manual work: {len(manual_tasks)}')
    lines.append('')

    lines.append('## Imported exactly')
    lines.append('')
    lines.append('- HTTP requests (method, URL, headers, query/body params)')
    lines.append('- Response assertions → `pm.test` (status / body contains / equals / matches)')
    lines.append('- Regex & Boundary extractors → `pm.collectionVariables.set`')
    lines.append('- Simple JSONPath extractors (dot/index paths) → property access')
    lines.append('- Basic/Digest auth, User Defined Variables → collection variables')
    lines.append('- `${var}` → `{{var}}` interpolation')
    lines.append('')

    lines.append('## Approximated')
    lines.append('')
    lines.append('- **Thread group load** → VU count + duration + one of 4 load profiles (JMeter custom ramp curves are not reproducible; closest profile chosen).')
    lines.append('- **Loop counts / iterations** → duration in minutes (Postman runs by time, not iteration count).')
    lines.append('- **JMeter functions → Postman dynamic variables** are approximate: `__time` is epoch-ms in JMeter vs epoch-s for `{{$timestamp}}`; `__Random`/`__RandomString` lose their argument bounds. Verify where exact values matter.')
    for w in plan['warnings']:
        lines.append(f'- {w}')
    lines.append('')

    lines.append('## Needs manual work')
    lines.append('')
    if manual_tasks:
        for mt in manual_tasks:
            lines.append(f'- **[{mt["kind"]}]** `{mt["request"]}`: {mt["detail"]}')
    else:
        lines.append('- None 🎉')
    lines.append('')

    lines.append('## Not supported by Postman performance runs (dropped)')
    lines.append('')
    lines.append('- **Think time / timers** (Constant, Uniform, Gaussian, Throughput Shaping): no equivalent.')
    lines.append('- **Multiple concurrent thread groups**: a run is one collection repeated across VUs.')
    lines.append('- **Flow control** (If/While Controllers): requests were flattened; restore with `pm.execution.setNextRequest`/`skipRequest` if needed.')
    lines.append('- **Cookie/Cache/DNS managers**: cookies are handled automatically per VU; preset values not transferred.')
    lines.append('')
    return '\n'.join(lines)


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(description='Import a JMeter .jmx file to a Postman collection + perf config.')
    ap.add_argument('input', help='path to the .jmx file')
    ap.add_argument('-o', '--outdir', default='.', help='output directory')
    ap.add_argument('--name', help='override the collection name and output base filename')
    ap.add_argument('--json-report', action='store_true', help='also emit the raw IR and manual tasks as JSON')
    ap.add_argument('--fail-on-blockers', action='store_true', help='exit non-zero if any item needs manual work (CI gate)')
    args = ap.parse_args()

    try:
        plan = parse_jmx(args.input)
    except ET.ParseError as e:
        print(f'ERROR  {args.input}: malformed JMeter XML — {e}', file=sys.stderr)
        sys.exit(2)
    except (ValueError, KeyError) as e:
        print(f'ERROR  {args.input}: could not import — {e}', file=sys.stderr)
        sys.exit(2)
    base = args.name or os.path.splitext(os.path.basename(args.input))[0]
    if args.name:
        plan['name'] = args.name  # --name overrides the collection name, not just the filenames
    os.makedirs(args.outdir, exist_ok=True)

    manual_tasks = []
    try:
        collection, pass_if_hints, extras = build_collection(plan, manual_tasks)
        cfg = build_perf_config(plan, pass_if_hints, extras, base)
    except Exception as e:  # build-phase failure: fail cleanly, never a stack trace
        print(f'ERROR  {args.input}: import failed in build phase — {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(2)

    coll_path = os.path.join(args.outdir, f'{base}.postman_collection.json')
    with open(coll_path, 'w') as f:
        json.dump(collection, f, indent=2)

    for role in ('setup', 'teardown'):
        if role in extras:
            with open(os.path.join(args.outdir, f'{base}.{role}.postman_collection.json'), 'w') as f:
                json.dump(extras[role]['collection'], f, indent=2)

    perf_json_path = os.path.join(args.outdir, f'{base}.perf.json')
    with open(perf_json_path, 'w') as f:
        json.dump(cfg, f, indent=2)

    perf_md_path = os.path.join(args.outdir, f'{base}.perf.md')
    with open(perf_md_path, 'w') as f:
        f.write(render_perf_md(cfg, os.path.basename(coll_path)))

    report_path = os.path.join(args.outdir, f'{base}.report.md')
    with open(report_path, 'w') as f:
        f.write(render_report(plan, cfg, manual_tasks, base))

    if args.json_report:
        with open(os.path.join(args.outdir, f'{base}.ir.json'), 'w') as f:
            json.dump({'plan_name': plan['name'], 'variables': plan['variables'],
                       'warnings': plan['warnings'], 'manual_tasks': manual_tasks,
                       'thread_groups': [tg['load'] for tg in plan['thread_groups']]}, f, indent=2)

    print(f'OK  {args.input}')
    print(f'    collection -> {coll_path}')
    print(f'    perf       -> {perf_md_path}')
    print(f'    report     -> {report_path}')
    print(f'    thread_groups={len(plan["thread_groups"])} warnings={len(plan["warnings"])} manual_tasks={len(manual_tasks)}')

    if args.fail_on_blockers and manual_tasks:
        print(f'FAIL  {len(manual_tasks)} item(s) need manual work (--fail-on-blockers).', file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
