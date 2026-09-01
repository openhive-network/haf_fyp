import json, urllib.request
for p in ("/haf-fyp-api/health","/haf-fyp-api/v1/fyp/algorithm","/haf-fyp-api/v1/fyp/metrics"):
    try:
        d=json.load(urllib.request.urlopen("https://testapi.hivescan.info"+p, timeout=45))
        print("  %-34s %s" % (p, type(d).__name__))
    except Exception as e:
        print("  %-34s ERR %s" % (p, e))
