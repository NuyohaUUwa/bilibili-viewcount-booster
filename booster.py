import sys
import threading
import random
from time import sleep
from typing import Optional
from datetime import datetime

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


FETCHERS = [
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


def fetch_all_proxies(quiet: bool = False) -> set[str]:
    """Fetch proxies from all sources, returns a set of proxy strings."""
    all_proxies: set[str] = set()
    for name, fetcher in FETCHERS:
        try:
            proxies = fetcher() if not quiet else _fetch_quiet(fetcher)
        except RequestException as err:
            if not quiet:
                print(f'{name} source failed: {err}')
            continue
        except Exception as err:
            if not quiet:
                print(f'{name} source error: {err}')
            continue
        all_proxies.update(proxies)
    return all_proxies


def _fetch_quiet(fetcher):
    """Run a fetcher with stdout suppressed (for incremental refresh)."""
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        return fetcher()


def build_proxy_dict(proxy: str) -> dict[str, str]:
    """Build a requests-compatible proxies dict supporting http, https, socks4, socks5."""
    if proxy.startswith('socks5://') or proxy.startswith('socks4://'):
        return {'http': proxy, 'https': proxy}
    proxy_url = 'http://' + proxy
    return {'http': proxy_url, 'https': proxy_url}


def filter_proxy_list(proxies: list[str], label: str = '') -> list[str]:
    """Filter a list of proxies for HTTPS capability using multi-threading.
    Returns the list of active proxies."""
    if not proxies:
        return []
    _count = [0]
    _total = len(proxies)
    _active: list[str] = []
    _lock = threading.Lock()

    def _test_batch(batch: list[str]):
        for proxy in batch:
            with _lock:
                _count[0] += 1
                idx = _count[0]
            try:
                requests.post('https://httpbin.org/post',
                              proxies=build_proxy_dict(proxy),
                              timeout=(connect_timeout, read_timeout))
                with _lock:
                    _active.append(proxy)
            except Exception:
                pass
            if label:
                print(f'{label} {idx}/{_total} {100*idx/_total:.1f}%   ', end='' if IS_TTY else '\n', flush=not IS_TTY)

    n_threads = min(thread_num, _total)
    batch_size = _total // n_threads
    threads = []
    for i in range(n_threads):
        start = i * batch_size
        end = start + batch_size if i < (n_threads - 1) else None
        t = threading.Thread(target=_test_batch, args=(proxies[start:end],))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return _active


def get_total_proxies() -> list[str]:
    all_proxies = fetch_all_proxies(quiet=False)
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

known_proxies: set[str] = set(total_proxies)  # track all ever-seen proxies for dedup
max_consecutive_fails = 3  # remove proxy after this many consecutive failures

start_filter_time = datetime.now()
print('\nfiltering active HTTPS proxies using https://httpbin.org/post ...')
active_proxies = filter_proxy_list(total_proxies, label='filter')
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

fail_counter: dict[str, int] = {}  # proxy -> consecutive fail count

while True:
    reach_target = False
    start_time = datetime.now()
    dead_this_round: list[str] = []

    for i, proxy in enumerate(active_proxies):
        try:
            if i % update_pbar_count == 0:
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
            fail_counter[proxy] = 0
            print(f'{pbar(current, target, successful_hits, current - initial_view_count)} proxy({i+1}/{len(active_proxies)}) success   ', end='' if IS_TTY else '\n', flush=not IS_TTY)
        except Exception:
            fails = fail_counter.get(proxy, 0) + 1
            fail_counter[proxy] = fails
            if fails >= max_consecutive_fails:
                dead_this_round.append(proxy)
            print(f'{pbar(current, target, successful_hits, current - initial_view_count)} proxy({i+1}/{len(active_proxies)}) fail      ', end='' if IS_TTY else '\n', flush=not IS_TTY)

    # Remove dead proxies
    if dead_this_round:
        active_set = set(active_proxies)
        for p in dead_this_round:
            active_set.discard(p)
            fail_counter.pop(p, None)
        active_proxies = list(active_set)
        print(f'removed {len(dead_this_round)} dead proxies, {len(active_proxies)} remaining')

    if reach_target:
        break

    remain_seconds = int(round_time - (datetime.now() - start_time).total_seconds())

    # --- Incremental proxy refresh during wait ---
    if remain_seconds > 30:
        print(f'refreshing proxy pool during wait ({remain_seconds}s available)...')
        try:
            new_pool = fetch_all_proxies(quiet=True)
            new_candidates = list(new_pool - known_proxies)
            if new_candidates:
                random.shuffle(new_candidates)
                if len(new_candidates) > 2000:
                    new_candidates = new_candidates[:2000]
                print(f'found {len(new_candidates)} new proxy candidates, testing...')
                newly_active = filter_proxy_list(new_candidates, label='refresh')
                known_proxies.update(new_candidates)
                if newly_active:
                    active_set = set(active_proxies)
                    added = [p for p in newly_active if p not in active_set]
                    active_proxies.extend(added)
                    print(f'added {len(added)} new active proxies, pool now {len(active_proxies)}')
                else:
                    print('no new active proxies found in this refresh')
            else:
                print('no new proxy candidates found')
        except Exception as e:
            print(f'proxy refresh failed: {e}')

        remain_seconds = int(round_time - (datetime.now() - start_time).total_seconds())

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
