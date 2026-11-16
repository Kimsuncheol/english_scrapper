"""Wake the public latest-series pipeline and verify its persisted result."""
import json
import time
import urllib.error
import urllib.request

URL = 'https://english-scrapper.onrender.com/api/v1/series/check'


def main():
    for attempt in range(3):
        try:
            request = urllib.request.Request(URL, method='POST')
            with urllib.request.urlopen(request, timeout=240) as response:
                result = json.load(response)
            latest = result.get('latest') or {}
            if result.get('status') not in {'new', 'up_to_date'} or not latest.get('expression'):
                raise ValueError('Latest lesson is unavailable or incomplete')
            print('Latest lesson saved:', latest.get('issue_date'), latest.get('id'))
            return
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == 2:
                raise SystemExit('Latest lesson fetch failed after three attempts') from None
            time.sleep(30)


if __name__ == '__main__':
    main()
