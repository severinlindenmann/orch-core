#!/usr/bin/env python3
"""Builds the hosted-preview folder from addons/dashboard/dist (the former HANDOVER publish procedure).

OBSOLETE since G2 (permanent URLs, Vite base '/'): the output no longer loads in the claude.ai viewer, which does not
serve /assets/. The hosted preview is frozen at 98151971. Kept for reference only.

- copies dist/assets/*.js (+ other non-css assets) into <out>/assets
- inlines every built CSS file into index.html (the publisher takes CSS only inline)
- escapes U+FFFD and C0/C1 control characters (except \\t \\n \\r) in every .js as \\uXXXX
- writes index.html: title, Google Fonts, inline CSS, dark background, dark class, modulepreloads, root, entry script
- prints a JSON {files: {...}, removed: [...]} for the Artifact publish call
"""
import json, os, re, shutil, sys

dist, out = sys.argv[1], sys.argv[2]
old = set()
if os.path.isdir(os.path.join(out, 'assets')):
    old = {f'assets/{f}' for f in os.listdir(os.path.join(out, 'assets'))}
    shutil.rmtree(os.path.join(out, 'assets'))
os.makedirs(os.path.join(out, 'assets'))

src = open(os.path.join(dist, 'index.html'), encoding='utf-8').read()
css_hrefs = re.findall(r'<link rel="stylesheet"[^>]*href="\.?/?(assets/[^"]+\.css)"', src)
preloads = re.findall(r'<link rel="modulepreload"[^>]*href="\.?/?(assets/[^"]+)"', src)
entry = re.search(r'<script type="module"[^>]*src="\.?/?(assets/[^"]+)"', src).group(1)

BAD = re.compile('[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f�]')
esc = lambda s: BAD.sub(lambda m: '\\u%04x' % ord(m.group(0)), s)

files = {}
for f in sorted(os.listdir(os.path.join(dist, 'assets'))):
    rel = f'assets/{f}'
    if f.endswith('.css'):
        continue
    dst = os.path.join(out, rel)
    if f.endswith('.js'):
        open(dst, 'w', encoding='utf-8').write(esc(open(os.path.join(dist, rel), encoding='utf-8').read()))
    else:
        shutil.copy(os.path.join(dist, rel), dst)
    files[rel] = dst

css = '\n'.join(esc(open(os.path.join(dist, h), encoding='utf-8').read()) for h in css_hrefs).replace('</style', '<\\/style')
page = '\n'.join([
    '<title>orch Mission Control</title>',
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap">',
    f'<style>{css}</style>',
    '<style>:root{color-scheme:dark} html,body{background:#0e1014;} #root{min-height:100vh}</style>',
    "<script>document.documentElement.classList.add('dark')</script>",
    *[f'<link rel="modulepreload" href="./{p}">' for p in preloads],
    '<div id="root"></div>',
    f'<script type="module" src="./{entry}"></script>',
])
open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(page)
print(json.dumps({'files': files, 'removed': sorted(old - set(files)), 'count': len(files)}))
