"""Best-effort, deterministic Groovy / BeanShell -> JavaScript translation.

This handles the common mechanical rewrites so the model only has to review and
fix the genuinely ambiguous parts. Every script that passes through here is ALSO
flagged as a manual task, because no regex pass can guarantee semantic fidelity
for arbitrary JSR223/BeanShell code.

Keep the rules ordered from most specific to least specific. The model may extend
this table; see references/script-translation.md for the patterns and rationale.
"""
import re

# (pattern, replacement) applied in order. Patterns are plain regex on source text.
RULES = [
    # --- JMeter bound variables -> pm equivalents ---
    # vars["x"] / vars['x'] -> pm.variables.get("x")  (bracket form, must precede .get/.put rules)
    (r'\bvars\[\s*([\'"][^\'"]*[\'"])\s*\]', r'pm.variables.get(\1)'),
    (r'\bprops\[\s*([\'"][^\'"]*[\'"])\s*\]', r'pm.variables.get(\1)'),
    (r'\bvars\.get\(', 'pm.variables.get('),
    (r'\bvars\.put\(', 'pm.variables.set('),
    (r'\bvars\.getObject\(', 'pm.variables.get('),
    (r'\bvars\.putObject\(', 'pm.variables.set('),
    (r'\bprops\.get\(', 'pm.variables.get('),
    (r'\bprops\.put\(', 'pm.variables.set('),

    # --- logging ---
    (r'\blog\.info\(', 'console.log('),
    (r'\blog\.warn\(', 'console.warn('),
    (r'\blog\.error\(', 'console.error('),
    (r'\blog\.debug\(', 'console.log('),

    # --- previous-sample result accessors (post-processor context) ---
    (r'\bprev\.getResponseDataAsString\(\)', 'pm.response.text()'),
    (r'\bprev\.getResponseCode\(\)', 'String(pm.response.code)'),
    (r'\bprev\.getResponseMessage\(\)', 'pm.response.status'),
    (r'\bprev\.getResponseHeaders\(\)', 'pm.response.headers.toString()'),
    (r'\bprev\.getTime\(\)', 'pm.response.responseTime'),
    (r'\bctx\.getPreviousResult\(\)', 'pm.response'),

    # --- sampler mutation (pre-processor context) ---
    (r'\bsampler\.getUrl\(\)', 'pm.request.url.toString()'),
    (r'\bsampler\.addArgument\(', '// [manual] sampler.addArgument -> set a query/body param; '),

    # --- JDK calls that have JS equivalents ---
    (r'\bSystem\.currentTimeMillis\(\)', 'Date.now()'),
    (r'\bUUID\.randomUUID\(\)\.toString\(\)', 'require("uuid").v4()'),
    (r'\bInteger\.parseInt\(', 'parseInt('),
    (r'\bLong\.parseLong\(', 'parseInt('),
    (r'\bDouble\.parseDouble\(', 'parseFloat('),
    (r'\bString\.valueOf\(', 'String('),
    (r'\bMath\.', 'Math.'),  # identity, keeps it explicit

    # --- declarations: Groovy def / typed decls -> let ---
    (r'\bdef\s+', 'let '),
    (r'\b(?:String|int|long|double|float|boolean|Object|var)\s+(\w+\s*=)', r'let \1'),

    # --- Groovy GString interpolation "${x}" stays valid-ish; leave for model ---
]

# Tokens that, if present after translation, almost certainly need human review.
REVIEW_MARKERS = [
    'import ', 'new ', 'class ', '.each', '.collect', '->', 'bsh.', 'ctx.',
    'OUT.', 'SampleResult', 'Thread.sleep', 'try {', 'catch', '@', '::',
]


def translate(source: str, lang: str = 'groovy') -> dict:
    """Return {'js': <translated>, 'needs_review': bool, 'reasons': [...]}."""
    if source is None:
        return {'js': '', 'needs_review': False, 'reasons': []}
    js = source
    for pat, repl in RULES:
        js = re.sub(pat, repl, js)

    # BeanShell implicit result vars have no clean pm.* mapping.
    reasons = []
    if lang and 'beanshell' in lang.lower():
        reasons.append('BeanShell script: implicit vars (ResponseCode, IsSuccess, bsh.args) have no pm.* equivalent')
    for marker in REVIEW_MARKERS:
        if marker in js:
            reasons.append(f'contains "{marker.strip()}" which needs manual translation')
    needs_review = True  # always true: no mechanical pass is trustworthy for scripts
    return {'js': js, 'needs_review': needs_review, 'reasons': sorted(set(reasons))}
