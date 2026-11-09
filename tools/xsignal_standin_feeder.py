"""MECHANICS TEST ONLY: appends DexScreener latest Solana token profiles/boosts to the
xsignal queue as stand-in signals (no X mention data). Not a signal-quality test."""
import json, sys, time, urllib.request
q = sys.argv[1]; minutes = float(sys.argv[2]); seen = set()
end = time.time() + minutes * 60
while time.time() < end:
    for url in ("https://api.dexscreener.com/token-profiles/latest/v1",
                "https://api.dexscreener.com/token-boosts/latest/v1"):
        try:
            data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "momo"}), timeout=10))
        except Exception as e:
            print("feed error", e); continue
        with open(q, "a") as f:
            for d in data:
                m = d.get("tokenAddress")
                if d.get("chainId") != "solana" or not m or m in seen:
                    continue
                seen.add(m)
                f.write(json.dumps({"ts": time.time(), "mint": m, "source": "dexscreener_standin",
                                    "note": url.split("/")[3]}) + "\n")
    time.sleep(30)
