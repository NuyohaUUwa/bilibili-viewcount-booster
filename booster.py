import sys
import threading
import random
from time import sleep
from typing import Optional
from datetime import date, datetime, timedelta

import requests
from requests.exceptions import RequestException
from fake_useragent import UserAgent

# parameters
IS_TTY = sys.stdout.isatty()  # False when piped (e.g. by web.py), use line-by-line output
connect_timeout = 5   # seconds for proxy TCP + TLS handshake
read_timeout = 10     # seconds for waiting server response after connection
thread_num = 75  # thread count for filtering active proxies
round_time = 305  # seconds for each round of view count boosting
update_pbar_count = 10  # update view count progress bar for every xx proxies
bv = sys.argv[1]  # video BV/AV id (raw input)
target = int(sys.argv[2])  # target view count

# statistics tracking parameters
successful_hits = 0  # count of successful proxy requests
initial_view_count = 0  # starting view count


def fetch_from_proxifly() -> list[str]:
    """Fetch HTTPS-capable proxies from proxifly's curated list."""
    proxy_url = 'https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.json'
    print(f'getting proxies from proxifly (HTTPS list) ...')
    response = requests.get(proxy_url, timeout=(connect_timeout, max(read_timeout, 15)))
    response.raise_for_status()
    data = response.json()
    proxies = []
    for item in data:
        protocol = item.get('protocol', '')
        ip = item.get('ip', '')
        port = item.get('port', '')
        if not ip or not port:
            continue
        if protocol in ('http', 'https'):
            proxies.append(f'{ip}:{port}')
        elif protocol in ('socks4', 'socks5'):
            proxies.append(f'{protocol}://{ip}:{port}')
    print(f'successfully get {len(proxies)} HTTPS proxies from proxifly')
    return proxies


def fetch_from_proxyscrape() -> list[str]:
    proxy_url = ('https://api.proxyscrape.com/v2/?request=getproxies&protocol=http'
                 '&timeout=2000&country=all')
    print(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, timeout=(connect_timeout, read_timeout))
    response.raise_for_status()
    proxies = [line.strip() for line in response.text.splitlines() if line.strip()]
    print(f'successfully get {len(proxies)} proxies from proxyscrape')
    return proxies


def fetch_from_proxylistdownload() -> list[str]:
    proxy_url = 'https://www.proxy-list.download/api/v1/get?type=http'
    print(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, timeout=(connect_timeout, read_timeout))
    response.raise_for_status()
    proxies = [line.strip() for line in response.text.splitlines() if line.strip()]
    print(f'successfully get {len(proxies)} proxies from proxy-list.download')
    return proxies


def fetch_from_geonode(limit: int = 300) -> list[str]:
    proxy_url = 'https://proxylist.geonode.com/api/proxy-list'
    params = {
        'limit': limit,
        'page': 1,
        'sort_by': 'lastChecked',
        'sort_type': 'desc',
        'protocols': 'http',
    }
    print(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, params=params, timeout=(connect_timeout, read_timeout))
    response.raise_for_status()
    data = response.json().get('data', [])
    proxies = [f"{item['ip']}:{item['port']}" for item in data if item.get('ip') and item.get('port')]
    print(f'successfully get {len(proxies)} proxies from geonode')
    return proxies


def fetch_plaintext_proxy_list(url: str, label: str) -> list[str]:
    print(f'getting proxies from {url} ...')
    response = requests.get(url, timeout=(connect_timeout, read_timeout))
    response.raise_for_status()
    proxies = [line.strip() for line in response.text.splitlines() if line.strip() and ':' in line]
    print(f'successfully get {len(proxies)} proxies from {label}')
    return proxies


def fetch_from_speedx() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt',
        'TheSpeedX GitHub list')


def fetch_from_monosans() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
        'monosans GitHub list')


def fetch_from_kangproxy() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/officialputuid/KangProxy/master/https/https.txt',
        'KangProxy GitHub list')


def fetch_from_clarketm() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
        'clarketm GitHub list')


def fetch_from_hookzof() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt',
        'hookzof socks5 list')


def fetch_from_sunny9577() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://sunny9577.github.io/proxy-scraper/proxies.txt',
        'sunny9577 proxy list')


def fetch_from_miralay() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.txt',
        'Proxy-List-World')


def build_view_params(video_id: str) -> dict[str, str]:
    """Return API query params for either BV or AV id."""
    normalized = video_id.strip()
    if not normalized:
        raise ValueError('video id is empty')
    lowered = normalized.lower()
    if lowered.startswith('av'):
        aid = normalized[2:]
        if not aid.isdigit():
            raise ValueError(f'invalid av id: {video_id}')
        return {'aid': aid}
    if normalized.isdigit():
        return {'aid': normalized}
    return {'bvid': normalized}


def fetch_video_info(video_id: str) -> dict:
    """Fetch video metadata and ensure API response is valid."""
    params = build_view_params(video_id)
    response = requests.get(
        'https://api.bilibili.com/x/web-interface/view',
        params=params,
        headers={'User-Agent': UserAgent().random},
        timeout=(connect_timeout, read_timeout)
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or 'data' not in payload:
        msg = payload.get('message', 'unknown error')
        raise RuntimeError(f'bilibili API error: code={payload.get("code")} message={msg}')
    data = payload['data']
    if not data.get('aid') or not data.get('bvid'):
        raise RuntimeError('video info missing key identifiers')
    return data


def get_total_proxies() -> list[str]:
    fetchers = [
        ('proxifly', fetch_from_proxifly),
        ('proxyscrape', fetch_from_proxyscrape),
        ('proxy-list.download', fetch_from_proxylistdownload),
        ('geonode', fetch_from_geonode),
        ('speedx', fetch_from_speedx),
        ('monosans', fetch_from_monosans),
        ('kangproxy', fetch_from_kangproxy),
        ('clarketm', fetch_from_clarketm),
        ('hookzof', fetch_from_hookzof),
        ('sunny9577', fetch_from_sunny9577),
        ('miralay', fetch_from_miralay),
    ]
    all_proxies: set[str] = set()
    for name, fetcher in fetchers:
        try:
            proxies = fetcher()
        except RequestException as err:
            print(f'{name} source failed: {err}')
            continue
        except Exception as err:
            print(f'{name} source error: {err}')
            continue
        for proxy in proxies:
            all_proxies.add(proxy)
    if all_proxies:
        print(f'collected {len(all_proxies)} proxies from all available sources')
        return list(all_proxies)
    raise RuntimeError('failed to fetch proxies from all sources')


def time(seconds: int) -> str:
    if seconds < 60:
        return f'{seconds}s'
    else:
        return f'{int(seconds / 60)}min {seconds % 60}s'

def pbar(n: int, total: int, hits: Optional[int], view_increase: Optional[int]) -> str:
    progress = '━' * int(n / total * 50) if total else ''
    blank = ' ' * (50 - len(progress))
    line = f'{n}/{total} {progress}{blank}' if hits is None or view_increase is None else f'{n}/{total} {progress}{blank} [Hits: {hits}, Views+: {view_increase}]'
    return ('\r' if IS_TTY else '') + line

# 1.get proxy
print()
total_proxies = get_total_proxies()

# 2.filter proxies by multi-threading
if len(total_proxies) > 10000:
    print('more than 10000 proxies, randomly pick 10000 proxies')
    random.shuffle(total_proxies)
    total_proxies = total_proxies[:10000]

active_proxies = []
count = 0

def build_proxy_dict(proxy: str) -> dict[str, str]:
    """Build a requests-compatible proxies dict supporting http, https, socks4, socks5."""
    if proxy.startswith('socks5://') or proxy.startswith('socks4://'):
        return {'http': proxy, 'https': proxy}
    proxy_url = 'http://' + proxy
    return {'http': proxy_url, 'https': proxy_url}

def filter_proxys(proxies: 'list[str]') -> None:
    global count
    for proxy in proxies:
        count = count + 1
        try:
            requests.post('https://httpbin.org/post',
                          proxies=build_proxy_dict(proxy),
                          timeout=(connect_timeout, read_timeout))
            active_proxies.append(proxy)
        except:
            pass
        print(f'{pbar(count, len(total_proxies), hits=None, view_increase=None)} {100*count/len(total_proxies):.1f}%   ', end='' if IS_TTY else '\n', flush=not IS_TTY)

start_filter_time = datetime.now()
print('\nfiltering active HTTPS proxies using https://httpbin.org/post ...')
thread_proxy_num = len(total_proxies) // thread_num
threads = []
for i in range(thread_num):
    # calculate the start and end index of the proxies that this thread needs to process
    start = i * thread_proxy_num
    end = start + thread_proxy_num if i < (thread_num - 1) else None  # the last thread processes the remaining proxies
    thread = threading.Thread(target=filter_proxys, args=(total_proxies[start:end],))
    thread.start()
    threads.append(thread)
for thread in threads:
    thread.join()  # wait for all threads to finish
filter_cost_seconds = int((datetime.now()-start_filter_time).total_seconds())
print(f'\nsuccessfully filter {len(active_proxies)} HTTPS-capable active proxies using {time(filter_cost_seconds)}')

# 3.boost view count
print(f'\nstart boosting {bv} at {datetime.now().strftime("%H:%M:%S")}')
current = 0
info = {}  # Initialize info dictionary

# Get initial view count
try:
    info = fetch_video_info(bv)
    bv = info['bvid']  # ensure BV id is normalized for later requests
    initial_view_count = info['stat']['view']
    current = initial_view_count
    print(f'Initial view count: {initial_view_count}')
except Exception as e:
    print(f'Failed to get initial view count: {e}')
    sys.exit(1)

while True:
    reach_target = False
    start_time = datetime.now()
    
    # send POST click request for each proxy
    for i, proxy in enumerate(active_proxies):
        try:
            if i % update_pbar_count == 0:  # update progress bar
                print(f'{pbar(current, target, successful_hits, current - initial_view_count)} updating view count...', end='' if IS_TTY else '\n', flush=not IS_TTY)
                info = fetch_video_info(bv)
                current = info['stat']['view']
                if current >= target:
                    reach_target = True
                    print(f'{pbar(current, target, successful_hits, current - initial_view_count)} done                 ', end='' if IS_TTY else '\n', flush=not IS_TTY)
                    break

            requests.post('https://api.bilibili.com/x/click-interface/click/web/h5',
                          proxies=build_proxy_dict(proxy),
                          headers={'User-Agent': UserAgent().random},
                          timeout=(connect_timeout, read_timeout),
                          data={
                              'aid': info['aid'],
                              'cid': info['cid'],
                              'bvid': bv,
                              'part': '1',
                              'mid': info['owner']['mid'],
                              'jsonp': 'jsonp',
                              'type': info['desc_v2'][0]['type'] if info['desc_v2'] else '1',
                              'sub_type': '0'
                          })
            successful_hits += 1
            print(f'{pbar(current, target, successful_hits, current - initial_view_count)} proxy({i+1}/{len(active_proxies)}) success   ', end='' if IS_TTY else '\n', flush=not IS_TTY)
        except:  # proxy connect timeout
            print(f'{pbar(current, target, successful_hits, current - initial_view_count)} proxy({i+1}/{len(active_proxies)}) fail      ', end='' if IS_TTY else '\n', flush=not IS_TTY)

    if reach_target:  # reach target view count
        break
    remain_seconds = int(round_time-(datetime.now()-start_time).total_seconds())
    if remain_seconds > 0:
        for second in reversed(range(remain_seconds)):
            print(f'{pbar(current, target, successful_hits, current - initial_view_count)} next round: {time(second)}          ', end='' if IS_TTY else '\n', flush=not IS_TTY)
            sleep(1)

success_rate = (successful_hits / len(active_proxies)) * 100 if active_proxies else 0
print(f'\nFinish at {datetime.now().strftime("%H:%M:%S")}')
print(f'Statistics:')
print(f'- Initial views: {initial_view_count}')
print(f'- Final views: {current}')
print(f'- Total increase: {current - initial_view_count}')
print(f'- Successful hits: {successful_hits}')
print(f'- Success rate: {success_rate:.2f}%\n')
