import os
import requests

CLIENT_ID = os.environ.get('AMZ_ADS_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('AMZ_ADS_CLIENT_SECRET', '')
REFRESH_TOKEN = os.environ.get('AMZ_ADS_REFRESH_TOKEN', '')

r = requests.post('https://api.amazon.com/auth/o2/token', data={
    'grant_type': 'refresh_token',
    'client_id': CLIENT_ID,
    'client_secret': CLIENT_SECRET,
    'refresh_token': REFRESH_TOKEN,
})
d = r.json()
print('Auth status:', r.status_code)

if 'access_token' in d:
    t = d['access_token']
    print('Token OK')
    r2 = requests.get('https://advertising-api-eu.amazon.com/v2/profiles', headers={
        'Authorization': 'Bearer ' + t,
        'Amazon-Advertising-API-ClientId': CLIENT_ID,
    })
    print('Profiles status:', r2.status_code)
    print('Profiles:', r2.json())
else:
    print('Auth error:', d)
