import os, sys, json, requests, subprocess

api_key = os.getenv('SQUARE_API_KEY', '').strip() or os.getenv('BINANCE_SQUARE_OPENAPI_KEY', '').strip()
print(f'API Key present: {bool(api_key)} (length: {len(api_key)})')
if not api_key:
    sys.exit(0)

endpoints = [
    ('v2_presignedUrl', 'https://www.binance.com/bapi/composite/v2/public/pgc/openApi/image/presignedUrl', {'imageName': 'cover.jpg'}),
    ('v2_preSign', 'https://www.binance.com/bapi/composite/v2/public/pgc/openApi/image/preSign', {'imageName': 'cover.jpg', 'fileName': 'cover.jpg'}),
    ('v1_presignedUrl', 'https://www.binance.com/bapi/composite/v1/public/pgc/openApi/image/presignedUrl', {'imageName': 'cover.jpg'}),
    ('v1_preSign', 'https://www.binance.com/bapi/composite/v1/public/pgc/openApi/image/preSign', {'imageName': 'cover.jpg', 'fileName': 'cover.jpg'}),
]

headers = {
    'X-Square-OpenAPI-Key': api_key,
    'Content-Type': 'application/json',
    'clienttype': 'binanceSkill',
    'User-Agent': 'BinanceSquareAutoPosterPro/3.0',
}

for name, url, body in endpoints:
    try:
        r = requests.post(url, headers=headers, json=body, timeout=8)
        print(f'[{name}] {r.status_code} -> {r.text[:200]}')
    except Exception as e:
        print(f'[{name}] Exception -> {e}')

try:
    with open('test_img.jpg', 'wb') as f:
        f.write(b'\xff\xd8\xff\xe0' + b'0' * 2000)
    env = os.environ.copy()
    env['BINANCE_SQUARE_OPENAPI_KEY'] = api_key
    cmd = ['node', 'square_scripts/post-image.mjs', '--text', 'Test', '--images', 'test_img.jpg']
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
    print('\n--- NODE SCRIPT OUTPUT ---')
    print('STDOUT:', res.stdout)
    print('STDERR:', res.stderr)
except Exception as e:
    print('Node run exception:', e)
