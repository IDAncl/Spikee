#!/usr/bin/env python3
"""
ui_app.py — a small local dashboard for the outreach bot.

Run this instead of Task Scheduler:
    python ui_app.py
then open http://127.0.0.1:5050 in your browser and leave the terminal
window running. It replaces the Task Scheduler entry - this process IS
the 20-minute loop now (it runs outreach_bot's `run` on a timer itself),
plus it gives you:
  - a live countdown to the next scan
  - drafted emails waiting for your approval, with Approve/Reject right there
  - a clean, scrollable pipeline: queued, waiting on a reply, an actual
    inbox of what people wrote back, rejected drafts, and finished leads

It's just a thin web layer over outreach_bot.py - same Supabase leads
table, same icp.txt, same LLM/Gmail/Calendar setup, nothing new to
configure.
"""

import base64
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_file

# On a host like Railway, credentials.json (Google OAuth client) and
# token.json (the one-time Calendar authorization's saved refresh token)
# can't just sit in the repo - they're real secrets and this repo may end
# up on GitHub. Instead, paste each file's contents as a single-line base64
# string into the CREDENTIALS_JSON_B64 / TOKEN_JSON_B64 environment
# variables (e.g. `base64 -w0 credentials.json` locally to produce the
# value), and this decodes them back to real files on disk before
# outreach_bot.py ever looks for them. Does nothing if the files are
# already there (e.g. running locally with the real files in place) or the
# env vars aren't set - totally optional, existing local setups are
# unaffected.
def _bootstrap_secret_file(env_var, filename):
    b64 = os.getenv(env_var)
    if not b64:
        return
    path = Path(__file__).parent / filename
    if path.exists():
        return
    try:
        path.write_bytes(base64.b64decode(b64))
        print(f"  wrote {filename} from {env_var}")
    except Exception as e:
        print(f"  couldn't decode {env_var} into {filename}: {e}")

_bootstrap_secret_file("CREDENTIALS_JSON_B64", "credentials.json")
_bootstrap_secret_file("TOKEN_JSON_B64", "token.json")

import outreach_bot as bot

RUN_INTERVAL_SECONDS = int(bot.os.getenv("RUN_INTERVAL_MINUTES", "20")) * 60

# Bump this string on every real change to this file. It's printed at
# startup AND shown as a small tag in the bottom-right corner of the page -
# if what you see in the browser doesn't say this, the browser/process you're
# looking at isn't running this file (old process still holding the port,
# browser tab never reloaded, etc.) - check that BEFORE assuming a feature
# is broken.
BUILD_TAG = "build 2026-09-26-no-scroll-jump"

app = Flask(__name__)

# Spikke logo, embedded as a data URI so the dashboard is a single self-
# contained file - no separate image asset to keep track of or lose.
LOGO_B64 = (
    "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"
    "jVdwXZ8wjFsQnkNwX44MWmz5EqotBQ9Cl8r4OIH0WHfaGer9132QfH8fg8ODVGslVOCnPK1UZSE1cvXBLa1fapotMP3XRJjx"
    "8Bxa/XfTPIRMxiTv2AzsP8Ajv3yQvZs2f8wsFCg6xYawopoBZnh86mKdA4GJZ2UqwylaCuhmzsR3XTZt2oRbrVLIJzWABsxW"
    "xwhaG7iUoua6aJrG+vXrWblqZeCFNfzIZ8JAhUnQyczVC5s9K/UBiLFS9PX1YNs2oR+mDDzZNMZp5tKAumRCcKl5NSrlMiJR"
    "feiwnJI4ShxbqFTHDKruQKq1KraZY8WKFTjZ7H8N/dbBmdMf6ungYF0IyjUXy85kTz5pLd3Fbqq1SgdDkaQLSiatAhoQBRGD"
    "AwN4VferhmGACtojfU1r1LSjqAWea7yXakC8iURS53LClJC5pkAFSZ8b7ddaXzddSgxDZ2BohI0bH2f04ODH8sU8uVwW0zQm"
    "MZpsx6KrmGf37t3vuf/+X7NvYB+2bWPpVsfnU/+qVCr4rsepp57KOeec80NhWY3ZZa3CzlJKnJRQMDQ6glQxpmW2ZKmysY+a"
    "kGYSVPX09jJ3bpK9+B0zpukysqlXMpl+7WHqOte+//3qk5+8Xl100YVq5eq1qqd31llhGFEeKzE2NsrA0BDlUikp26igufdk"
    "YkVVOmVhZGzsvw0eOEDSC9d6NmQjgKnfW61WQ0rJiuUrKHQX/oxY4jaQB3UENqrdDrhuFb9SYc6c/lM/cf316kPXXavOufAc"
    "tXjlsscLuYLp+zWqpVFKo0OMjo7W2bnte3lCwHuYqvmdiY11oxRHIa7rE6uYdWecpq744FVkZ/cwMDqAG7upWneywLIDxaOj"
    "0CYzV/A4Niyy9n/plk0261Arl3n8t0/wygvPC8NycBwLdHVUUlTqGDm6hJ469bu0PvRWw1qP6Oq0ctuw0U2Tffv23bxl81s3"
    "nHLaOizLxvf9lqbnybJHURBQq9VYuHAhZ599Ltt37N7gltwntKLWGHAopsgMD6ffTgiBJgSBG2AYon/FihX0dPVQTqcVt+q9"
    "yZbPkamEj2lqHNy/n73792PoOkLX24wHWlPHUGlxqmGnpqifQOiGyGLIosWLWbRgwd+9sWnTV1zfI2M5bWr01EsoHQc90taT"
    "J4BQStxKlWXLlldPOeUUpEio9u30ENW80zRKNk0TiWT/wAA1v/bPhmWgjBb2mU6DTZvQ7aM2SKoxuVh1wq1Vy7On7b4ab6/p"
    "WYAw0toK+BMzWj9W9BgmcRAlpBYzkTCrT1Nu2xrptThOFs8Leel3z4kXTl6rLr/8ckw7ud8w1eekhUBWv6NyuUJvXy/vv+Ya"
    "BgcH1csvvyxc103rRs0m4Wy2gFuqcvDgAGEYY9kGuqYnwzAnTU9N/lKtVunp6eW0007nhRdenu/V3H22abUNjDxco1pfek0T"
    "eK7L4iVL/Pe85z0sWLCQc87ZR6VSYteuPc9s376NAwcOsGPXWxt379x/gVet4gE1y8Zx0hFHdbaNEKCb+F6N/QcOEkUxhmEh"
    "VRVNtPh11T7PrlQtsWjRIs4685ybH7r/ge/5Xojj0Dbx+ogsq4iJgySoWLv25BevuOIKlJTs27+f4dHR8/fs2B3s3b+XvXv3"
    "8tprm04qjQ6+7kUBIDBsh0wm01jjifXBwze/osOFRhF+zSUKQ0486aTffeC66+iaP5eDowfxAhcV1U9se8lxOnckjtJ1iaN1"
    "ZIZNNpsj8nyefvxJXnzuKWHqOjkng2FazRab1mWZRs9IHYI5eTwyt+mxdTUBolA42Ryj46VPv/z6SzesXLMC3bBQLb09dckj"
    "IVRbVSwMQzKZDCedchJrXnl543PPPCtiFPl8ttGpP6lheyqYra03KVlkDYGIBLXIpeJVOXHFiQMnn3wyANUoSKSkWhRf6hh7"
    "nXxgYaEZNqOjo4wPj4KmYUpFU0wjIawo2WyKDjs4sdY6pRQh1UqFuXP6Oefsc9mzZ/f/5ZVr/9HqMpp6jTQF/VUHEnCrvB2p"
    "Rl6tViNj2vPPPPN05ixcQKVSJYgidCEmi/U2xBhM8sUimqYxNjJGGIT7cvlik50o/v/tvVmwHOWZJvx8X+61nTpH+wJaEBIg"
    "ARK7xCpAAoxt3Da2x3Z3G09M3/yeiJ4/wjM3PRHjvui5mL7pieh/bv6OsCdixh09btvQGIQkFkkgFgkhCS1oB0loOWvtuWe+"
    "c5GZVZlVWXXqLAjhdkWcQIdTVbl937s+7/MwwBfAeTD+EomPBs6QteFN00qr/dV4AqfthbB+P5mVNysgAcxalkUoihYGFrzn"
    "zhQECflcAWNjI9izZxdWrgxmzzzHgRU/TluIaOsmJsQSli5ZgkceeQQnT56cV6lURiVJghDCzKNAAACOnzi+9ejxYzvW374+"
    "yPpdp2P+Lzqrer2BfL6Ae++9D/v377+0b98+pus6NE1rft90IlzHcVGv18G4gDtuv0OWNQ3j4+Mg8lAoFHH33XPwwAP3o1Yr"
    "48yZ8w+eOnWGyuUxTIwM4+TZc6xSLkNSNUixvZ3NZNCoVXD4yGHc/+ADWLxoAXRLgmvZHc2VqD1VmSjjxqVLseWJx3DwwP7v"
    "ToxO/No0zQDV2PeIFmuRb4f72baDsRAtV8D9998D13XRqNchijIWL1yEZTfcCEWRMDY2gbNnzx47ffoMyuUySqXSvnPnzt0f"
    "yQfNsGeWnjIzAIZpwbItLLrhhh8/89w31y9YcSMqtRJM0wDZ6d2SycqHPYqbXXsxUytfdX/JqoR8VoMoMBw5fAS7d+9hZsNE"
    "JpeFGAoABhXWVi9vUgaOWc4dZ9IrSy0eE0ERRfi+hxMnTuLS5SsYGspDjkVBrVJbHDwQDFwbhoEF8xfh6S1bsHzVKmpMlFAv"
    "14I+heO00HOpTpZ1ySZbqga62UC1UsHixUusZ7ZuwdKly1CtVgHbaZZC/TigIYauEmUBEhNQLlVRrdd+KskSSBRbkStDgCYQ"
    "WeBgPEqgGFv9wth98AHTMMF8jvV33oHb1q39ua43UKvW4YaaWPFVwajtJ/73EGzRaNSh6zUsu2n5pQ0b7oLCGYxGPSyHUQLl"
    "6VOrxyWKBEkUcOHCBZy7cB6cCZAjhC2PgjMGEAueHXPh8hYcv2MfhdfoxebK/UmCyWDiIfqsFzjN2PNo9vpENCVi4hNsrBdW"
    "kjHIioJcvoAzZ86wnW/sRLXRQC6fhyzIiZ5P84cINhxYuoVGo4E77rgD99577wg5NqrlagcwKTdnDsavjOx8e9ceVCt1aJkM"
    "fF+I9ShbP34Y9JTLNQwNFbFp00PQstlHjXoAZpiuUCvnDHqjBqNew6pVq2jjxvvAfGBiYgITExMYGRnG2FgZ5XIZjHGsWbMK"
    "3/3ud/CXf/n/4i9/9jNs2riJPCJ4jpmYac1kMpAVDSdOnGRnTp2BIMiQBTlcR63r82PX6Dgeag0da9bcgnvueeD/AIBe0+F5"
    "dthCZc1WavoP66gYeZ6HRrUGANi4cRMtX34Tao06xsoTmJgYx8jIcBBwVirIZDK4//5N+PM//3P87Gc/w1/8xV/ct2TJkh+6"
    "rtsEwcTtCJ9q1JCGlTAbJkxDx8CCedLT3/7mL1etW42qrkN3XcBNV2plfZYRp89aNvWf5hElETlFg8wFnDp4BLt3vH6fPVZG"
    "NqNCkuRYiB0YWt6nM5uJE4sb97TjENGkaMZeJb4IJcgVBZLEcerUKbbvgw/geoCkaQl4NUVy8BT78X3oug7HNHHr2rX4sx/+"
    "AOvvXk+OZ6NcKqGqW/BcN4Bw2yHqrF2bqaOkF5QVHddFrVJBpVzCQCG//enHHpPX33UXHMsMGCHaDFmipweAiSI0UUGlWsGn"
    "n52Cruv/Q5XVsF/ImzxyHnmBIxMCwtPIUfheCJTw0XHOruug1qigWCxi88OP4da1t5JH3jzDMGCYZl+kzgwEz3VgW3VUJypY"
    "MH+htfWpLbhh6RI0GnqAkoz3tpAEJPi+D01RUK/rePfdd3Hx3LmimlFD3kneQlNFzWgQyOPgHjVBGS3odISUDH5ccpos6hF3"
    "YQxhnnIlYY8JwX3zfB8eJeH5IYodfkQpwgVwCB0woLQfLZsFF2Tsf//D4kf79sH3/YDqy0/2/5pIRM+HbZuo1+sYKhbx1NNP"
    "YdENN/7e0uswDb2JagQQjDUAOHr0+F9cuPgpREGEogpteytpMRwnUEG/++712Pz45l1aLg/LsGAYJpxQRmoyK9UMvFjAb2qb"
    "JvLF4p1f//qzWLp0KXS9Ds9zwXlAJGzbJsrlMsrlKmq1GnS9gVxOgyCKqNQqkDiHwJLK5yx0aI5lYv/772OiXIamKQHdFXWz"
    "nMBEpQRBkPHEE49jzW23EeDBMGy4voeIXaYFS2/7idkqokA12zBqADysvvUWevqpLVAUEXq9Bma7EAQOURRh2xZKpQpKpRLK"
    "5XGYpok5c+agUqng8uXLv/J9P5H5MhYOdE/anI6Z7c5sisE2HRiGjszgALY8vdW+fcPtmKiWUanXQI4TmyHr/OGT4HpmmIfE"
    "JlGm4MhECXlZgyppOHv8NLa99PLWic9H9ufyBYiihvjwaTt2sqdrpukOErRgxEhEO10OQpNnY70cnAhAkhRYeh379u3D6dOn"
    "UchmAiceKzX60WxXCN0h8uE4NhqNEizTxLp1t+PP/uyHeOzRR2ne3Lm/8F0Lo6OjKJXG4Hg2yPMCmDQC+raozMcYh+fxAO1U"
    "rWJsZBjjV67AMk1pxcqV9O1vfXvrQ48+GlQDGjrcZhTcnl+0TjYjScjm8zj36Wc4fPg4AD8U5owB9YLKR/ARD/DgN4FJLYZ6"
    "6sxOCDBMB3qjgZtvWYPvPf8d3Lb2thHLsjKmXodlmQELAxEY+W0/BM4ceL4L27BQK0+gUMz/4umtW+V777kHvm3DMvXQRfgp"
    "xtoPyXllKFoWIyNXcezoMViOU1GUTLNsmTBRoVqwxwIl7fiMlh86nUSQ4rmBA+XUlLwh1hqjaqvzyC1EXaCe5jfFOmP3rxkU"
    "+eF5+QkYVsJXIzGeBVEUkcvnUW/UKq+9/gZOnT4JVVGQy2mJ9dmk6QqPZZomqvU6li9bjkcfffRZWc2iUqsFShzNtoKIwmAR"
    "ExMT//DRwUMwTRtZrQDBlzoYMqI947o2KuUqBEnC1595Bpsfe5SI+6iVx+G6Flw3GiBv//ET0jG2a6FSLaM6UYKsyXj++W8f"
    "2nD3Bhi2A8vS4bh2AqgRBFIudN2AYTRQKlWxfds2vP3OHkbMhyyJ4bgQa7YSZFkGZzLe3/8hO3jwQDD2kM0jifFJWiXHdDE+"
    "Po6bbroJP/7xj7H61lvJtUzUq1W4jhurUrTNmsVmxlzXheW4qJUnYBsWFi+/cfsPfvAjrLrpZjQaOmqmHm2RhEyV67qwLAuu"
    "6+HChQt48cUXMT5yNUDttvmtKdJZdTo017Sh1xuQshlsefJpuu+hh1F3HZQaOuC7ifry9UNe1rtXFWyMHC6c+xw7t21/ceLi"
    "5Z0DxTmQM0rCOEQErlPOsVJ8S3svZWr9MzYjIEx79AbOkM1l4bkuLl26yN5/ew8tXLAAuVwetVoNjmOFvpxS6dhdF9D1Coh8"
    "LFm6FM899xzWrl37wokTJ184duzYpbGxsRXjExOO59kQQ4kPxkVwzhKK7H442JPVMjcvX3XjqVvWrsWGDRuwcuXKAKlWrcOy"
    "zdDQtzE1xJhDSBCQUVVUKhV8dGA/Rq5cYXImA0EUWsEOhaXFsIfIedhr84O+oN9qFiYgz83fHAcW2ZBECTevWoOnnyYIHI1j"
    "R45tLY2P79RyWaiKCkmUmtyr5BEcx0HDDgRpOQlYumwZPfzww7jvnnvgkY9arQHXsVOz/haThg9ZlaEqEj49ew5Xrl76nqpo"
    "4ZxUS5OLxXiLOhCR8ZZBRJvkB0YoPg/IJ9lFHJAIBGJeYtA1vqYDurKwf+anM8UQemezGS0Dt+Di0zNn2O49e2nxwhuQzefg"
    "WD4sx+zcWCFASm80MDg4iM2bH8H5zy7Se3v3sHKjjGKx2ORilNUCGg0D7+1954U1a2755V13rYeckaHrdgrrRNChNYwGiHwU"
    "BgvYsmUrGpZOe/fsZdVSBYJqIqtlIIpiWzbhwHYIvhcYbLMRAHwWLVn6iyeeeeKFhx59DPB9GOEIQHLPtqCYnIvIZgsYHr6M"
    "Dz74AHA8qPlsoEHH2rQaAWTyGdSrZfzLS78/tnLFTWtvvOFGmKYNyzKSoCS0EixdN0HEsGbNzfjed7+L3/z2N/TJ0WOsPD4G"
    "QZahKCoEQQRjcXfiwvMc+L4Ho2E2KUnu2HA3ffvb38Lq1atQqpSDa3OicCZwvH6MP9T3CZmMhg8++AAHDx5kAIMsy6kIYXG6"
    "BtC17QCSLUl46KGHaeMTDwMKh1mpgpGXMlnOrkuHlnBkWQ2FXAHjY2PYuWMbLnxy4k+UbAaKzJshfCRGiA727n7uGzoFHKk3"
    "0rG53amT2zCh7UZTB4CkvY+zgEctNzSE8vAwdr23d+vcJYt2PL31afi+j4maB+46SShe28t1A1ix4zjI5TTcc889uGP9nfj8"
    "wudLjh8/bn928QIqEyXopgnPcQ8xzuD7fhnwbYd8XRKk9blsdvmihfOxZvWtWLN6NebOmxeojNs2LNOEZZvNaJ+zNiARtTZx"
    "VlUhSQo+OvAu9r333t96RChoWlIsFUluC8/jYMyOSYIkHwq1ByUM8FyGeqMOn3ysXLYC3/rmc5g3NG/HwY8O2GNj47nqRMkJ"
    "+O2CniS5BDWjIJfP/z+D+UV/s3zFyuLDjz2M226+GbZLaJTLcGwn4WDizz4yrIIsQhIFXLp8CQeOHELVbPy6mCs2nVT7dfLI"
    "DHoRm0myJhAfwueMN/tdvK+QNCgg+qHKNDXJbFsuquVEWaxkynvI56bXXDRZgyGaOPDhvvW3rVlzaOODG6GoEgzbaBnieMXD"
    "J/iWhUatgQXzF2HLlsdx7tMzfz82NvbvTcuCEjoaUfCRLWQxfPXq/3zttdd+uWTJAsxbsBCu68I09QilEwvogqMYRgO+72Px"
    "4oX44fd/gCULF9Ped9/+7MrV4RXVUmnSa8rm8pl1d9zZeHLL47hz3Tq4vo9KpQI7RP3FHUx0ZZIkoVAoQpKAt97ahU9OnmRc"
    "EiEIUsLZxl+apsG1bZw/e2bd7rd20w9++AMUCgVUKg4sy22zZ2F25oTsM+TjjjvvQH5gAC+/9DIdOnJ4Sb1Uuqzbk5AFcAlD"
    "c4d+/MADD/5yy5YnsHz5cpTLJeiGDt/zWs6Lt+CcAXWXhDlz5qBUKuHNN9+EUa9CyeSCqgq1idJimtyMjmnBME34AvDAxvto"
    "8zNPgUkSxibG4Dhuq8wzO7JOs56JtffsJACipqKQyaM8OoLtv38NJw8eYoKiIJPJgAliQoWsGSnHSN77d2jU+g7W5/kSdciL"
    "dJ1nmUbE0CL6bG1/gYBsLoeJSmnnju2vn1q4cOnqDXfeCdd1oOsePM9tg9vHS50BKwgA2LYOSdKQVXNYtWoVli1bBs+2oTsW"
    "yuUqPNddH6gkhI6EfIgcyGUHkMtnkMtkw1KegUapEUC5267dp/SnrKoq8pqG4ZGr2LdvHyrV6n8amDMIWRQTBp4iBvoI3OG5"
    "gMA7yIMT1IYAyPcCRvbQEFqWjYbfgCiJWDh/IZ752tdwxx1r5TOnz9lnz30K29ZDfTsOkTgWLpqPZcuXY968eZg/bz6KAwWY"
    "joNGrRGySYSgijhfa4gijZxCTgn6mXv2vo0jR488mJG1UCQ1tiQSIwtCS0uTU0evNdERbKI/Wiw/3fe0D2KwGUVZmR/jt2wl"
    "8YyhCRDxZxDeioqIfCGH0njl8M5db2Dx4qW4ccUSqKYMMxoub7simwh+rQJBkbD61lvxyCOP/HTbtm3/XjcMqJwHsk0EKGIW"
    "2YKP48ePszff3E3PPfcNFAo5+K4LyzUTRYlmoM6CUY0Sasjn83jmmaewdt265adPnaLjx4/iyqVLr5iO82tOzPEBKIq2JZvV"
    "XtA0DQuXLMRtt9yG1TevxlBxCIbVgK6bsCyrGTM3YwyfAOZDEiVkMhpyuTw++uhDvPHG6z93DQOZfAGiLKYgO1uGfyA3CNM0"
    "sPPNnWzlquV0/30bIQgyBMGD63qJa4vmNx3HQ6k0Ade1sWjhEvzoh/8Gm84/eOnjjw/js88uYGxi9Ke+6+7lXJQB1xZFaX0+"
    "X/i7BQsWFleuvAnLlt2IG2+8EZlMDqOj42g0TDDmJkE4PgGMgzFAlgVkszkwxvDyyy9j//79DFxsCnOmBetTdmbMcWA6Jnzf"
    "xbq77qTNz25FbmER4xOjcBwnmYBfZ46sWylPVFXkswOolUrY8ep2HHr7HcZEjly2ADk2p0NN5WKWlBCj6RyfppSrxg1r6/nN"
    "DNDfXAis7UzCjEuVRORzeVy+eGHNv/zmt6RIMlbctBy2bYFsCz7jHZumOVMWfrfvMziOAd02IDVUKJIEWVYxIEnIZ/MAYwmY"
    "recHES/5PlzbQbVUhWmbSScWLizedCx+h2NXRBn5fB7VRgNv7dqFwx9/LKuaBk3NJAiOW/c2YMnwfcBngECtQKK91E5EkGUZ"
    "lunh/PnzGBgYwKLFi+A4gaKA4zrwAWSyKtasuRW33LoOjXoNRkOHIEggkUFVJGS1LCRRhOO6sCwX5VoDhmEjGMhMAfYwBs5a"
    "yn6yIiNXyOHjI8fw3gf7yo5pv5srFsLrS1dZ50KUCwXwfKCbZlvAQt/UM6Pe3iwEg+o+cTDywCKC2A6D2kJlMuaDkQdvmutX"
    "0zTYGQufHP2E7dm7h767+DvIZrMwPBPMZYkSauRUHcag12qYO28eHt28GWfPf0oHP9jPGpKErCQB5EMUASWjwrJs7NixkxXy"
    "OXpyy9MoDhZRrZVhmlZL1DRy0GDwyIKtW7BtHblCAatvvhm3rFmDzZs34/KVy88alv2sQAyME3KZArScDEXWoCgaFFmC7bio"
    "1CswTQuu68a2YqyCEPJAcUlGNpvHp+fP4Xe/+x0uX7z411JWg5bRwCajtRN8ZDNZNMoV/OM//tPfFPLFv1q9enXAOuPZ8D3W"
    "GidBso9VM+twXQfZ7ADuuXsDNmy4HdWqjuHhK/+fYVmQBLF5jgsWLMKiRYFElGU5ME0d1eoEXNcKr6VzORH5kCQRiqIil9Pw"
    "7rvv49VXX2WebULJ5MBDmr20itMUe2YIhqJtGytuW/NPT33zGxhcPB9jpQkYDb0d03QdFRC7OzRRU1EoFGDoDby9ezcOvreX"
    "gQED+QHIopT+PR0zUjPrW/VzFe0EqS0NMDQjw379GmMBmzyLS/+mJXWiCE1RQNkcTp45yV5+8UV69rmvY9XKm2AIAgxdh+s5"
    "QEyOo+koKDncy2wGGyYsQwdRpXUIKW66g4aS2ASFBM6Dd7m4oHHtdzgcWZGRz2UB38fevXux5+23X/AZc3K5XMCKwFiHSabQ"
    "0DMOsKZsZ+dIP4XHFEUZvuTi+LHj4BLH17/+DWRzWdTrOjzfhWno0Bv1QCZGUyBICpSsFoBciACXUClX4LsePD8QQ00F7cRb"
    "gSGxMGNBb7dQLKJcqmLfu3txdfjKYE7LBjD1xMQdSw15PC8OrWAptEPhvFiClqrXGufNI/oEWATwFAb5AIzQYtHwITQpjKfM"
    "rUeEfD4L2zbwwfvvP7tm9a2v3HfvXciredTrdUSEvsmh7YCBvlauYWhgAA9veggXzn327yqVyj+IsgxZEAAKOC4HBwYwOj6C"
    "V17ZtjVfKOzY9MAmaLksPPJhmVZb7Ne6RsdxURqfQL1ag6apkFUNSxYuAhdFcB4YcSYywCVYXkA4UKtaaOgGGO+U447bUlEU"
    "wcBRKBZRKo3j1d+/jGNHjzEuqcj2UtVO7BsfXJEgeBKuXLz4n//5N//8Vy/823+LJQsXo14XUK3qANyws5KcbWQug+k6MIwR"
    "qIoCRRaRLxSRy62G45ix0qQDzgXYto1arQ7D0Fu8i/E10YZeJvKhqgUMDhZx7Ngn+M1vfrvPqFfBRLk56N41W081nm0DngwA"
    "OQ5M04JhG1iwdOG9W5595nsLlt2IUqMGM1Ez7Tbp1X34kq6BS6P27rkoQpVl5LMZNAwT7+x8C++9s4eBgOzgAERRTo9wImON"
    "eOOckv2raYBpJs+gGNIpjPuHfibBHixdoLqt5ChKErJ5AZIh4+Tpk8z8jUNPb30S69atharIqDXqcAwjADU0dYza0C7UUqVr"
    "IaFZiMjqfEZOYpibwjJbq0GceBYhBIoxBkEkqFIOuYEsTNPBnl27sOP11/+u0Wj8z4GBgUQGSB1FshBfGwEXQuRYoszIWohR"
    "gQHZbB6WZeGj/Ydxw4qVeGTTJoAxmGYDphm4aNdxUXFcgOtgYBBBgeRMWIrjjAcUUyxlb8f2UzSmIAHgsoz8QBG27eL113fi"
    "wwMH16uSBFVVUktLretl8MJQ0/Nbs17Rc2rqgkX8fl5Kry1lcTvhdvABO9oBYkq+3HQssb5ZNHNHUy0wRPItkoRCPofxsbFX"
    "33hjB5bcsBBLFiyEY1swLDtlgYfAG38CHivi9rVr8eCDD/7/27Zt+we9VgPLFiEKoT6WLGCgWMT4+PDOF3/30t/Ylv1Xmx58"
    "AAP5AdRQCwSHHafrXjPNcHSEVZqBgCgGIKl4RSTYE7zVruhiQARRQEbNIDeQR6lUxquvvIpdb+9hPiPkC9kmN+fkjXtAEiXk"
    "iwNoVOo4eugQ+6df/Yqee+45rF69GpIko14vN3toaTefMcCwLOiGAdt24RGH61qJ9eGGrQjGeBgksmQ7oglOClaXogjIZoeQ"
    "y2Vw7MQJ/Pr//BrnTp+8X5DVDkeW1tYRe6LuYlbGNh3olo783DnY8s1v7rvp9ttQalQCrjPHbXIb9srJaPK1OeW/s0kseYcT"
    "C6MbLZeFpuRQGhnFnl1vYv+etxgsDwPZAUicg1Fg4GL2qwVliWkyxfFYUX2bzaoAZ2LstpUQtkH0u3IvdpNfSZ2FSJEvD8uA"
    "ckZG3a7j1InjrFIae2diYsuD999/P3KFAmxJgW7qsD0LzGFd6Z9aoMBYxsbQtczVfLaM0g19jK5K4iJUOYPcYBZ63cDOna/j"
    "1W3b1uu6fnju3LkBAiq+EViKjhMLSpdRdSp4HyVQZNG5c+ZDEGRkshlURsfmb3/llZHBfB6rbr4ZXJAhiB4chwPcSQQvTnRV"
    "HGAQWiuI0uLwZEbkAxA0DdmcBtuxsWf3HuzZs+c/mLZzuFDIQRTlBEN900M0laD9wCkRgZEL8hDkoJQS8hHBS5QJOZr6HW2i"
    "ZJFuHGOspVKAdg24SJKHNXFDvu83FeVZMzWjyeMySq5XTVGhqFkc/eQT9sF7H9CzX/86FEWD43ohR6SfWD9EPiyP4Pk1DM2Z"
    "gyeeeAwXPv2UDh44wEQuQsxlm+tKFEXkBwcxfPXyf/7n3/zzr6u16qGtW7egWCyi0aiBgGA8JN5Di2wD522ZIYs5snjvO8yl"
    "iXUx1gRRkVDIFZDTVHw+fBkv//YV7HnnbeY5FrKFApR2Royu5Jlh8E0MAgnIZosoWyPY//77bHx8/ODzz39//bp165ArDEDU"
    "TTQajdQGDUWETpyjETEEUaftaQLmKJYpgRKaZJLkQ1FyyOVyABj2HfgQL/32pUunPjm+FEyCqqp9UYSJqeuEJVkLHNNCw6pD"
    "lrN4/Nmn6Nb7NqBm69AdE9zxkBR4SL+flNqtmgmzYX9do/j4BJMkSBKQlXNQsxmcO3kGu994Hac/+ojBJQxmCpAgA8Thh1xV"
    "FDqQeNEpMLRxeBFrRR2s3404xRA0lIOImN77w3qw9N9Yd8WYjjJAZAcFH7lMFgJxDA8PP/S7f/ndposXr+69++47sGLVKgwN"
    "DsJ1LDQaOhzLC8qPCbUASqDMmuXItlyR2jLgRO81tisYYyBRhEwMoiogl8vD83wcP3oSH390GPsO7n/B9d3Dc+fMhaqqHeu7"
    "49r9ILXwwjIi8bQb1NKP83xAoICFHwIb/ez8efbyq6/Sk08+iXW33QZN01A3DDg2g2s7oESWlfJ8E5IZlAxWCOAkIFPQUCgU"
    "MDE2hj179+LtvXt/XqlW//tAPhBO9OPrMv790Vr2WvfaJw6PeaE4ZwQqYUl3KgBJnCfvunDDeWynOSrhU7IknlayjZSf+XT6"
    "v9Ty14KIgYE8JsolvPfBB/975YoVP1q79haoqgrTrMNx0q2O47io1+pYNH8xNm/ejHNnzw5Uao2KklEhiWIADpMkyJIEgQmo"
    "VEqHt23bxnTdoIcffghLly6FrDioVmowDCcsjlMyOuoK6grCGL8VFQfPLsGVCEiCCFlTMTRYhOt6+PDAQex8/U0cPvwRA0Tk"
    "CoXE+p5qjCyIhHwoxHvu9OkN//iP/+u/PPzwIz/fuHEjFi9eDFVV0WjU4LoBQUAcPc3aRHUjJe52M0ft+zkCe4CgKAoKhSwK"
    "hTkYHR3F7t078eabu781fPnKS1yWoamZvqnBxG62NiKg9V0PutEAwPHYM0/Sw48/ibJTh6k3Aun5ybdpilmlKbqk6Ts6agUj"
    "UASOgXwRrufhxKGj2PbSi/9h9OT5/86zGuYMZIKST7PSH0cItDn4DscUm4uJlQQprcxH6WXGxKLo6GFF0XyrfzTT5I/aEDqs"
    "rQfIYn2liGPPI4KS4cixAhr1yrtvvPEa++Tk0bfuu+fex+7ccBfmzx1EoTAEIgeNug7XMeF6FDazkxIiyb5ap55S9IvfNnNE"
    "hMDQiAIyGRWSmoFjGBguTeDjgwexd+97GB+9yrgkYmhwDiRJbANRpE0KJX/zicC8IDKPmuDNp8qC1I08D+QRBIFD1bJgAnD+"
    "3Hm2+63dZFoWbll1C/KFPDRZhWWYsHwDtumm1+miTCeOwIv8iSBBEjkymQwkVcWly5fx+vbteG/fvvnkYVTN5yGrSlw8pzOz"
    "i/pkQujQGMHjwTV6nIO5EdSbNfuWTABEJrUg9DwAxnQTg2UABMYdEECcIAjt2lMU2HowiDI1xw1Y1Kj0ImvUP+df61yC6kEh"
    "l8XnFy7+6c7X3/jR/AXzMXfufFiWDc7by2UuADFUWHfR0A3cfsft2PTAA+Xtb77JLMsKEa+tNScIAgYGiqg26vj9719h589/"
    "Ro8//gRuWb0GQ0NzYBgN1Ot1OI4bIH0ZRzd1baI2jluW5OgUBBGCEOiQqdkcOA/orN7f+y62v77zWyOXR16S1AAAIkrCzJB2"
    "BEiyhDzPwfZkfH7x4l//6lf/668vX75ITz75FBYvXow5c+bCMIxgT7su7CazSaB83bQUrN1itajM4gGqJIoQFRGKkkUup8Gy"
    "XHzyyQm8tv1V7H1nLyPXgaxpyKl5kNC9rNh3mZGIYFkWPMsBh4D7Ht5EjzzxJCAKsMbrgT5U23wPm7a7+uIm0BgAUVahyDIG"
    "Bgoolybw/q69OPrRobVjZy4c54qMvFZo0vS0ujPp58w6AmqKzfKwDoEH6nKprcHQtkIpY5PcknZY/uQy7SyFtqpbP42xLtfQ"
    "LBmJ0BQBglCAXrdw5fLlza9v34ZjHx+l1beswPLlq7Bo4SLMmTuEXG4ePC/QJHIcF67nwPeC2SM33mdtRnMspTcZlIVFUQw2"
    "gSRBlmUwQYBh6rhw6iROnDyFzy58ik/PffpYtVLdnc/noaoqBEFI9m0YegZcrd6Qh2AXUVvRq4Vg84ngs0D3TGAckiSBCxxn"
    "zp5m4xNjvzix8vgLt99+O1bfcjMK+QGopMKWTFi2HRgEIsAJwDOM2hr8jEEUBAiiCE3JQFREjI2O4uiHH+LIxwdx4tgJ5noe"
    "8vk8ZEVNXGOvEj8HwGQR2XwBxYEBeJoLxZADPsXmt3BIAgMTBCiagoyaQSaTD8ALPfYZ5wIkVZKUTAaFQgHMZwC8GLcjD8W9"
    "fRBxFAYLqNVMqKoKSZDCLLB3nyd9d7dKmaKoQFFUHDlyjB0+fIS+9rVvYO5cBaZZh+8LoaPwAT86mAdwEbIsI1cYxDf+5Dso"
    "1xt05MhR5rpOgDwNsw1RFOGKQAEF6HUdR44cYSMjw8+tW3fXi3feuQ6rV6/CnKEhWLYLxzFh224z+4NLcMOsLT7gIwgCXBdQ"
    "JQYmCZAEAaKkQRYFSLIE13UxPDKKEyeO4dDHh3D88HFmGjrUbAaKokIU+SzZyOBcZEmGQALq1TJ27dnDLlz4/Bdr16574Z57"
    "NuDGG29CcTAPQ7cheiJcywvY773QZnguuNzJlMI5IAgcRByiJECRRahqFrKsoFar4JNPPsHBgwdx4NBHP71y/uL/AAA1m4Mk"
    "aSCBwqCgPxyA2K2s5VgObNsBJ+Cu++6lp77xDUgZFaNjo3AdD+At7hGasnv5YjpK1OaAFEmEkstAVbIgx8ZnJ07j3b178fE7"
    "7zF4QLZYgCZoAEfH0GbPvhf1Ee6kvC1dbDKZqbBux2DdEZOTRS3pf+/M7jrmqhhro4xBU79OEhQMDKhQXRVG3cDZ82fZpSuX"
    "IMn7Hl0yf/6uNbfeiuXLV2BgIIeBgSKyuSyKykDA9+faYdnCBXkuXPLgeSzR4+GCAEHgkEURiiyDmAjbMlFvVDE6OoqJUgln"
    "zp7Bx0eOfDY+Mr7CEwiyJGFozpwWQ0DcEPa57LwAahGXN4yV/1oZbaCYHJolMewrKjKYZaFerf7k4MGDP7nw6adHL5y/c+2y"
    "Zcswf/485AeKyOaz4EwAuS4s24bneU3lPUkSIIgCJC5BkhhMw8Lo+AguXb6Mg4cP4+jRjx+0LOddVVSQyUpt5RfqCnmKMI1C"
    "iNSrVKu4dPEifI9gxfgj446J+wRVUyEqEur1MlRRbhIWd0bEBFkVAWIvjo2MPKuoMkxdDxGT8SUshP91UW/UMDY8Cgacihz4"
    "9C1ACAASBAwM5DA2VsJ77+175YYlNz47f+E8VKsBIw3nAgRBAmdhD63pnjk0TUWhkMey5ctw6uwZuE4A0W8qCjAGCRKgAIqq"
    "wDANTEyUXnrrrR3sww8/uPnuuzecWnPLLVgwfz4WLFiIefOK8MiHGwYvnufDcXwQPAhcCJyHLEEQAEkSwZgM23ZQr1Vw9eo4"
    "qrU6hq9cxkcHPsa5syeZYRjggojcYBGqLMeiYpoekI4l4wHGGESIkDQRkjwXhmXi3OnTPzl3+vRPDhw4+LONGx/42xtuWIqF"
    "ixdi/tz5kPMqOA803TzPjenXIXwWIgSBg4kiFDG476ZuYqwygitXRlCv6zh69DD27dv32OjVq7sBQFYUKFrAXtOOhehnfbDi"
    "nHmxN4db2AXqRgOuZWHN2ltPfv9Pf7h67rIbMFoqQbcN+I6T0CWjhCvpbtzZNJ0Z9eHIIkB1kMdKKMgiFFWDpmRQnpjAe+/s"
    "xaH9H64d//zycXgM2VwOqiaCQUYrUW6BDoixVF/CUuqnrRvNktQDXdZPArQe8x/pDyxm4FmY1jOWQq3TLyoyqsx0Fn6J/JB4"
    "t/X+oKHvpQbIkbNwGQNZFhzHgWPb8GwPosyRzxd+PDQ4+MuhefMwf95cDA0MYKBYxODgEDLZTJjNiJAFBi4G2RYHh898+K4P"
    "3dBRrVbRqFUxOj6BsbExjI6NojRRwvj42I8qteqvCAySpEDNBBpOLQ0tpKu7dnkZlolauYyHNj5Izz//XTAWMDAkVm2sB5TL"
    "5SArKl57bRt27NzBgrmYHAgE13NhWTZsywikS7LZv587f/5P586di6E5c7Bi2TIUikVks1lks1kIkhAg3xo2bFvH2NgoRofH"
    "MVEax2fnL+DyyOXv6fXarxlxqJkMRCHIVJNkrt2VASNkqE0ER9cxVCw+OjSY3+U6HjzXS91x5DMIogBFkTA+NrG+Vqkf5pIC"
    "sUv7wrQsMM6xaN68X0iy+IJt280eXGeJ24Msq/Bdf9fI2Mhm13YhS2r31TxJsMYSemwMer0OlxiWLFn431RV/Y+2bQdZeqQE"
    "3exzR7/yQL1alVGpVtdevXLlOOfdrzXKzh3HREM3YYVac9lcDosXLz16000r1w4OFpHN5zB/7jzMGZwLUZagKBK0TAYMCNZ1"
    "owFdD8qTlUoDY2PDGBkdxujI6L7x8dLWWn2i4hgumCghm9UgSBIkQei5oJvxJmOd2nNtoBJKFkJipXyCy1x4hoNGvQZQ4Ggk"
    "VcUNS5eUbrrp5uK8eXMwOFhEsTgPc+YMQQqJCBjjYAKD59gol0uolOto6DXUag2MjY1hePgyxsfHf1kqVX5SqUzAd3xwSYaq"
    "qVBkJWZr+5s1SiC0I2cW99T1Wh2uaWDpihV/+fz3v/93q9bdhrHyBGq2CT+iMmIt4tDuhcX+UQ+Ta5exjv4SjyO9REASFKhK"
    "UFKUuIDy6DgunPkUR48ewscHDjCYBDmnQZUUyJIMRi2Jgo6CKWOdSVHaeaYwcbB4BEt9XCSLsdv1AnYw1pJ1YFPtm7VH6q1r"
    "bIH+Kb1f1sxy0krGrciVR0wIpgPLMeB5PlzHgwsfnBF4ML/3X4bmDP1cVTOQFQmyrEHOapB5IOQZHdj3fVSqZZTLFdQq1VdG"
    "x8b+xPU8h5gPCQK4LEGQFWiKDEEUQ/aA9kvt4c3aKrWmaaJSLuOhBx+k55//DgCgWqm04NNtcz+5XB6qImPba9uxY+cOllEy"
    "0HJak3mCccD3bNi2D9OywZgPy/Igyjwzf87QjvzA4IOFXAEDxQIELsAwDOi6DsMwMDY2smN0dPQpx3ZDh6JC0RSIsgiBhCbD"
    "er8oXhbr4TquC8ey4DoWXMcLEHQ8huJokv8CTGBQBAngHIqqQhTEnuvTsiw4lgXHdeG7KYIxEe8mBVmiIImQRBmaIIK6eQ5Q"
    "nz2h5BWbtgXXtmHYNuB5YIIA8txgUJcjagCiiacKZ0ZEQUBGy0CSpfT9myijBcGf53swDRuOY8J1fXiuA7Ag21u0aP4vBgfm"
    "viDKEmQ5IERmjKNSKaNUKkHX9V/WauW/qlYalx3LRPOkBCCjKJBkBQoPRVYJPfRHOpGebNIKTVpfv+3LGODZHmzHhKFbALlh"
    "NUJEJqNi7txFf79g/ryfqhk5lsUG+2liYmJXtVr/r/V6dWej2miBiRgPytiKDFmWIIoK/JAxpiUDmHRmfZHhF+fMTYACbMuG"
    "UatCLGTx4x//Ka3fuAnlyjgmqrUmvXg723yXLsyUYBqpc1cxUt+44Y1ekiBCZAxyNhvANzmH7zoYHy3j+JGjOHn442NXLl5Y"
    "Z5SqEBQJiqZClbVm05h1rSCznoFB75Q3puoMavOPrIczS4+wWeggOue0eVD16XdYmiaPIJpoQhYrMVInLL79aTXB27GIN5qf"
    "gRsMBLuh4KXrevAcF47twiMPvu+2hDpZzBMwgAscoiCCcxGiJEIRgr4UlzgkUcY0CyxJdE3opU3LRrVcxqaHNtLz3/wTQBBQ"
    "qVbAuqyTXDYLWVGwfft2vPHGG0yVJGi5XLO8mVD3BsF2TDhOoIrsui5cz4XvufBdair3CgIPyzMiFEWEKEgQRAmSLLfYyNEC"
    "xcyoPO8yeIE+U1fgFGcAiQKE2LHbblvqevLa1ZlTY1oGRiIEgZpCqlN1ZO2ZRlvdDCyUD4pkb2KwhNTvjXpjCbBQHLQVDnwn"
    "g6WYmgUA23Xh2uEzdi04TlBliJxAx/2KSo6CCEEM1K0FWQhK1xATZM3p3pX1SGZpymahk7CbwsSBwfVceK4H33dg2x4c2wb8"
    "PvhbBDEo8UoiJEGEIAhgclC2bZFhU0r7g/Wl+hHrmbVYAgwniC4K84fwxNNP0t2b7kPFKKPq6ICQ3ETU0fCZ7q6KcwomDSeL"
    "7RrmB0UEQRIgSAo0VYakZaAwDsNxUauUMDY8jNPHT+Hs6XO/vHj67E9g2OCigFwmD0WWQJKYmOVIdx4zf/WMIiZNUlnXKku7"
    "FFmTeWCy2QTWvfHbqjGwFqku9doMKcY9+jwhySVHwUIWRBFi7EO264RDtgHCzefpRpUxBi6KYCIgQkK7Hm6/oJd0I9meYgQa"
    "XOQBPuctGYuU4IIQzGD55HbIDKWtAyKCKCoQJYZo9NN1XZDrwvMpxnQCIAS6JEZBfD+1YD+dzReMlTCQSOBdUBeso6RG/eGN"
    "wzcJgth/9aWv0+89oZpaYA2dkCQIHeASIo6+D9vOxEGpaK5mECMKAiQt5P6kXKg84DfpqRLGN+oVhvNsiUv1+6guzNhasf6q"
    "OEQBxZsgBNk5FGgawSYHZEf0ZG6HWee81QsV2zNvat/DrM2cMPQrTxZzZq2sxw+JLTc+9BA9/rWnoZONMb0OmzyQ6MYCEink"
    "MZhFIEcoHyByBh6W2wTGoXARsixBFmSIihQoDjs2DN1EdWwMRkPHyeNHcProyR3jw1efMksVwCIwhaFQzEPiEljYUOyoIV9L"
    "yq32gcr+puO66w1QVPOmKU+jt8+SRQPKDOmNQepIllmCrXoyo9NO+irGDYwULsMUwEny8/6USMO6K1r32NhEzalpQsiD1947"
    "jaRJyYfrteYrXXTqK3XYxNj/EwQBEITg8lM06KhvWNF0liI19fV6Q0emYTa7clrO9MzZJL34LldKU3SNvURuaQr7PFrrogBA"
    "6Dr029zCfq9DUk+0wIxOdtLmDusIIiPnLUII929rD3ftatLUg66pvsRIX4gxBg9AVstgzW2rcePKm/H52EUsVGRwj+AJIQks"
    "tYQ1+3l5XR5SgLjkbdG9Dy+ECZPP4Lo2OHnwTUKlVoVumKgaDVy9+jk+v/A5Ri4P/22jUv5P+ngJaAS8SGJWhVZQIfOgjMGm"
    "JC7RBgJpK+3Fh0DbHQJjbPJUqD1CZqxrV7Cd1b09L6KY+m9iUqprKbN3/blJmhrjhuulfNDLSaTGkpNlkH0s4C+a9owxHkDz"
    "vWBWiqiTjSI6B48Aqbl8+fTsxtTzqlmtFtAXeC+vY47xaxe/TgpcQW/c3Jd+AegQYCTCdcsgL8ajCc45TNfBh/sP27YkyXXT"
    "gucEkgcevCa0lghI91F+H3uQtx4kD2ZRgv/L4fsuLNuD5zpwfC+YyanpqBoWJkaG944PT2w16hUdtgU4HuD4AHFoqgS5WIAg"
    "SiAhTuXD0D3H6K8y1+25dRKzUu8sgMVq7ohJwNAkJcBUZ8u6nBubwaKNZY6h82sxFXRUFHvG8d2FsKl3uTkp2vTlvrzeBomD"
    "4Hu8GdgR+V3X2fWw9bsxu/zxNdvWH7N6n7/swIBiBA6tjPu6dWatSFzOZGHpJt7d/Y7y7tu7AccNefrbfBUPN7vQ5+Pw27a0"
    "kPJ9kQHhbU+R2ms1ACQJqpKHlBegMBk87OdFJbnrUQaUgU2x7DWd8gvrz0N3qw11K/PFwDcR6qsFEEFsVKDbuVGfu6bz33Qt"
    "HRsR4AEeB8j10LNTRB6Y73Swh1zvr8QYSfuz6a7wOvVofDqf+eMr5cl8cYocf2gpthgPhGUwSBkNpm7BdtyQDpu1PE70Ri8Z"
    "vbZnXQkxv9hXNPOkaLaFh17NpeDLGAePk0MKHLIgBb0zUQYXGMBEQAr8YDDE2hoPYKlzF3RdKlz3iutYl4ysP4fW73uov5Q0"
    "BUrJUv1Q1H+La67Nwk6IoQM7DfLkWXP3jDmdqp14QFNF4N2pp0Agj4MEFsqYAALrVHX+sspX/TixFkt/zFh2c0A0cztIk2Xm"
    "f0B+r5/nkzbLz9LsJV3b2xMFqVEVibGvzn0X0UYhxABkMiqyTAvna1qzWH7sXrPUW8wmXeGdN6czbZUANGNeCuBxia6EH/Hk"
    "9SOl0h/Q4ot3VDQl0MfsZGr9OjTWPYOLygwx1utm+6sJ3Y8PoMWeW+KwNGMDkVYqm1mm21kO9DwOTh48TulPI/zV44EMTNTr"
    "BZevyYqabvmq1dsNH0iMKDYBUKEv6Cqmdd5fPQ83FUcW/btDiqmDXeGLzc7aA76m8nyC+PvLqyT0u+a76JlFmkYtMk/GqM8s"
    "oYeB6VpuTSgQJhM+arup6R5xGvz7NM2b2+93X+8hDeteR2gDjICSUzotAapWIaSlVZUkDmYxksvJ5scnWy7tizoeRXauPzb1"
    "500A54EjIzckHO7yPZxcMOJNSRsO7ytheiPFB0rWSmIWlvoAbM9G6NC+Gr8Ke+YalGZYtxCUzRqadarnxKKqA9Gk+NGpPP1e"
    "fdz43/oNWMV4GMBmdYZhmkWJDtKASbhBwnmhyfOzNiM7ySeoLZvqRzvs+nBo/R+XAamKyv2URrprJHSin+I9tXiwyWZ4Kc1y"
    "SGLBs84BdQqLzb3CzBBdSy6BucEcWeqcWVh+8Ykn4NQBCpdSptu/IlkETUVOd+rtlD/87hnNzs6ltGIAfTmOLO5UeipVTv37"
    "+kp+plh5EYmurfPqnjayrtaL9bRw7URULDX/YGjnCEhLE1kqf+TUHdn088X+XNBkDmfyaCbBM9pd5jj1ADTFxUZpBr59bjl9"
    "BqLvEklHqSRWJmnK3UxigQUhKGZ7zUHh9Kw1GJp24TMhmULS5AoG15k3m9ZKm1Iv7I+vGbhEurazsP0GPrPwPe12Y7LWwYwk"
    "YGZcyriG0WkiwGSUcEks4hpMaEalUUbFu64s0Qvv4CvrGT2waQKQ+uWlmsEzmIWsId5SmUp03j5EHEifJB1aggh5mg4NTUme"
    "VquBiDpPvv3lC2A8EEohn+B1YRhpUnd5UeYWyat8WbHzjCLLjgXeT7acCCf76hHNFLz0r8udzVb3/CtTFejjb1NwZpO9kV2T"
    "i5hudtOLmJeFxL8RrNz3OkeQWQcAojsLQGqS0aWBOvXr7ee9bEr3OpWkY0YOjXWWRVjStfXVtE25kR2fYVNfVzxeh6dkVh6x"
    "6XcLtJID6V6ohpt+uzwCROpzgXyFMrMvwnx2z/AST+ePfgy4LkOiL7PSMEU6q34X0rWYd2CzshoYayuJ+QhpitLS+BTOtXA8"
    "ILUYGuv/dNos1hP80rVkQKxj0r6f+8RSYrn293X4h6agaltBdrooueieR8FDm5zN5BFY2OMimtXV0MrxWMJ0dlxr83l5YABc"
    "TvACOebWfSWWuH8R+SuxOCsjS2Uq/8Jj7Gk8t7Rifpto+pTCrY6if5/nxLr0WTsyla4ZNc26lemfVmGS6sOksRzNcKWnf743"
    "L2z6MD+bLKi8Bk5vNhymOH0Or34rGezL9exhU568qV0J9eAZbBnqdib76e6U2Jh9n8+CpTC/93NAIoYWOnsKvbKpGMhY/XHS"
    "5x+BM9IEtiPnMJXziwnGMpbQ7OidIUZ9ski+hBM8zwdn6bNjvu+DXD/I3siHjy9hxpSmjy1LC0iiuK9fR5BG94VpV16oxyVO"
    "Y65uGh+7to6MUqWUpn+mrG9HRj1qXl81RzaJ0vQs5FfTLrN9kan7tTkQJWm20T9tK/XFXMLaymLRYGP/t/kaoSyn1lBr5VKs"
    "GwRoeg4tdUOncGIyxsAlEaqqIjeQgyRIUOQMIu3pRCBBDJlcFqIoQVOVkEn8y3nNypQdzWptZJavhKb9TdPCtv0rqXbSV8Bu"
    "TyMz+2KcWJoHZewPr9mbzM7i7LxTcyisjxJPtxkUAvpWn24J/iQgmrOLSKPpfaTJIM4w9Yysj7JK6v+PJCq4CFVUwYhgNAz4"
    "mg/XteAz1pGvcI9gGBycO/B8D5KowIf7lTIAX9Rs9HVnrL8Ek9OPmSOwqXUWrtm5z07J8Us596bS9AwcVfvf0yRG+v2eNGPN"
    "prFaJj9O715hD6WslCFdTOP6Ur6jXzg9481jNe8x+oOFN/kruZDAbfTsdbD+59YY71J6maI1iN+OnrlkzBFPexCCcXDGYNs2"
    "NE0bGBgY2OX7tN6LlInbnRkQKiVLn1Wr5Wdr1cZxWZWvfcBP06dqm4kza84KpqkK0Ay+tGsZiaZsM2iaRYieEjCTPMl+r705"
    "PhN+HUt0D6cDhgms5FTKjDQN2z3pfZv2Mr5Oy4wsTkL7pWZMX04m2H7cfnW14gaizXW21RZbE2JEyWPQVJ5RuAf6YlPvE6FH"
    "sxXyE3Xoj6eWH1tok7hoWn8D2W1nTgAkWUa1WqsMjwxvcGyn855Qi8yaQBAEAaqWQUZV8cfXH19Td0Dtv335WEb6qs1Kxl6z"
    "4sy+6NR0ul2eL9Ohpd+bHr0fSlOyjbGQxBoBgbGmaUWuLJbusuY/6PosOUVo1DhKsEcUOe1HHR2HfGiaAk1Te2aM1PE0adbX"
    "zdTN4fVTpprN/R+XG7q+DSxLAo56Jkdpc339IDuvre36V+fM+nFkM3EohBRukFRo9cyjo+kWhtISl06pDbSRhqa4auqeN7CE"
    "N4sDzannNmCTXj7FHCKlQPnDN/azyFkP9NlMnhN1oj2bfHHNxIwmLTPSZMeg2B3twpjP0m4uY9M2FjPRGaPprPJ++qM9Mtvm"
    "Wo9QvTQ1zr2Z7NHoOV+/WVZvOHM01tE5q/5HXsrrJjNLi8hmMyuiHjkNm/aQKpvmEft37EjRMEuvifdYzAwpCm2UrOaxyZXe"
    "2yHAFKM3iUuhdxovliCZnpFDmklQQ60CX/O6Uhk+umBwaIorg7FZvbZuPabo89OeNJrKiGgb3RdNJ+NjsZiM0tf+rDqcBG78"
    "S9T2mpU8ORmWxzVu4zONXwQJ3h96VjYjZ9YvJ99sOrRrV2aZAvluxKLOONBFPZqQpFNKvy8pDo114SmLZytTDAgSZ0XtTix9"
    "Vo2I9Wnb2TXYbKxF099rtoeuxeZPDuj3C+C51uc51WU+Gf4hcmSRc75mpXwWV2j4ar4SQT+LFwU6HfW1zNf+EBTI/y8V1O1n"
    "dGFlwAAAAABJRU5ErkJggg=="
)

state_lock = threading.Lock()
IDLE_PROGRESS = {"stage": "idle", "current": 0, "total": 0, "label": "", "step": ""}
scheduler_state = {
    "next_run_at": time.time() + RUN_INTERVAL_SECONDS,
    "last_run_at": None,
    "last_run_summary": "no runs yet",
    "running_now": False,
    "progress": dict(IDLE_PROGRESS),
    # Bumped by 1 every time a scan finishes (whether or not it did
    # anything) - the frontend compares this against the last value it saw
    # to know a NEW scan just ended and it's time to pop up the summary,
    # rather than re-showing the same summary on every poll.
    "last_run_id": 0,
    # What the just-finished scan actually did, one entry per log_activity()
    # call during that scan - a draft written, an email sent, a meeting
    # booked, a lead marked dead, etc. This is what the popup shows.
    "last_run_events": [],
}


def do_scan(triggered_by="schedule"):
    def report_progress(stage, current, total, label, step=""):
        with state_lock:
            scheduler_state["progress"] = {
                "stage": stage, "current": current, "total": total, "label": label or "",
                "step": step or "",
            }
        # A real per-lead step (liveness check, website fetch, ...) can
        # genuinely finish in well under a second - too fast for a human
        # watching the dashboard to ever actually see it before the next
        # one overwrites it, or before the whole scan finishes and the bar
        # jumps straight to "Finished". This tiny pause after each named
        # step only affects runs driven from the dashboard (the CLI never
        # passes a progress callback at all, so it's completely unaffected)
        # and only fires for actual named sub-steps, never for the coarser
        # per-lead/per-reply counters - so it costs at most a fraction of a
        # second per step, not per lead.
        if step:
            time.sleep(0.35)

    with state_lock:
        scheduler_state["running_now"] = True
        scheduler_state["progress"] = {"stage": "starting", "current": 0, "total": 0, "label": "", "step": ""}
    bot.reset_activity_events()
    try:
        with bot.LEADS_LOCK:
            bot._run_once(progress=report_progress)
        summary = "scan completed"
    except SystemExit as e:
        summary = f"scan stopped: {e}"
    except Exception as e:
        summary = f"scan failed: {e}"
    events = bot.get_activity_events()
    with state_lock:
        scheduler_state["running_now"] = False
        scheduler_state["last_run_at"] = time.time()
        scheduler_state["last_run_summary"] = f"{summary} ({triggered_by})"
        scheduler_state["next_run_at"] = time.time() + RUN_INTERVAL_SECONDS
        scheduler_state["progress"] = dict(IDLE_PROGRESS)
        scheduler_state["last_run_events"] = events
        scheduler_state["last_run_id"] = scheduler_state["last_run_id"] + 1

    # Deliberately AFTER the state update above - that's what flips the
    # dashboard to "not running"/shows the finished-run summary, so website
    # prefetching for a FUTURE run only starts once THIS run has already
    # been reported as done. Never shown in the dashboard itself (no
    # progress step, no stage) - just quiet background upkeep from here on.
    bot.run_prefetch_after_run()


def scheduler_loop():
    while True:
        with state_lock:
            next_at = scheduler_state["next_run_at"]
        remaining = next_at - time.time()
        if remaining <= 0:
            do_scan("schedule")
        else:
            time.sleep(min(remaining, 5))


# The last successful get_pipeline() result, reused whenever LEADS_LOCK is
# busy (see below) - so /api/status can always answer instantly instead of
# stalling behind a scan that holds that lock for its whole duration.
_pipeline_cache = {
    "drafts": [], "queued": [], "waiting": [], "rejected": [], "inbox": [],
    "done": [], "attention": [],
    "next_company": None, "next_person": None,
    "queued_company_count": 0, "queued_person_count": 0,
    "drafts_company": [], "drafts_person": [],
}


def get_pipeline():
    """Splits every lead into the buckets the dashboard shows. "waiting" and
    "inbox" look like they'd overlap, but they answer different questions:
    waiting = whose court is the ball in right now (by status), inbox = what
    did people actually say back (by stored reply text, any status).

    do_scan() holds LEADS_LOCK for the ENTIRE duration of a scan (correctly
    - it's protecting the Supabase leads from a second scan or an approve/
    reject writing to them mid-run), not just around individual reads/
    writes. If this
    function blocked on that same lock, every /api/status poll during a
    live scan would hang until the whole scan finished - which is exactly
    why the progress bar used to look frozen: the request carrying the
    real-time progress never got a response until it was already over.
    Using a non-blocking attempt here, with the last good snapshot as a
    fallback, means status polls stay instant throughout a scan; the lists
    below just don't update again until the scan releases the lock (which
    is fine - the progress bar, driven separately through scheduler_state,
    is what's supposed to move live during a scan; these tables catch up
    right after)."""
    global _pipeline_cache
    got_lock = bot.LEADS_LOCK.acquire(blocking=False)
    if not got_lock:
        return _pipeline_cache
    try:
        _, _, _, rows = bot.load_leads()
        state = bot.load_state()
    finally:
        bot.LEADS_LOCK.release()
    leads_state = state.get("leads", {})

    drafts, queued, waiting, rejected, attention, done = [], [], [], [], [], []
    for r in rows:
        status = (r.get("status") or "new").strip().lower()
        kind = "company" if r.get("_table") == bot.SUPABASE_COMPANY_TABLE else "person"
        item = {
            "row": r["_row"],
            "company": r.get("company") or "",
            "email": r.get("email") or "",
            "status": status,
            "notes": r.get("notes") or "",
            "kind": kind,
        }
        if kind == "person":
            item["contact_name"] = r.get("contact_name") or ""
            item["job_title"] = r.get("job_title") or ""
        if status == "draft_pending":
            item["subject"] = r.get("draft_subject") or ""
            item["body"] = r.get("draft_body") or ""
            drafts.append(item)
        elif status == "new":
            queued.append(item)
        # "awaiting_booking" (Calendly, no zigzag flow) belongs here too -
        # it was missing from this list before, which meant those leads
        # silently fell through every bucket below and never showed up
        # anywhere in the dashboard. Fixed alongside adding the two new
        # statuses below.
        elif status in ("contacted", "replied", "awaiting_slot", "awaiting_booking"):
            waiting.append(item)
        # needs_attention: the reply-handling agent flagged something it
        # won't touch on its own (hostile tone, a legal/compliance mention,
        # or it just wasn't confident how to handle this reply) - it also
        # emails you directly the moment this happens (see send_alert_email
        # in outreach_bot.py), this is just the dashboard's persistent copy
        # of the same signal. awaiting_referral: they declined for
        # themselves but we asked if they could point us to the right
        # person, and are waiting on that reply.
        elif status in ("needs_attention", "awaiting_referral"):
            attention.append(item)
        elif status == "rejected":
            rejected.append(item)
        elif status in ("booked", "dead"):
            done.append(item)

    inbox = []
    for email_addr, ls in leads_state.items():
        if not ls.get("last_reply_message"):
            continue
        lead_row = next((r for r in rows if (r.get("email") or "").strip().lower() == email_addr), None)
        row_status = (lead_row.get("status") if lead_row else "") or ""
        inbox.append({
            "company": (lead_row.get("company") if lead_row else "") or "",
            "email": (lead_row.get("email") if lead_row else email_addr) or email_addr,
            "status": row_status.strip().lower(),
            "subject": ls.get("last_reply_subject") or "",
            "message": ls.get("last_reply_message") or "",
            "at": ls.get("last_reply_at") or "",
        })
    inbox.sort(key=lambda x: x["at"], reverse=True)

    # queued/drafts are already in table order (load_leads() reads each
    # table with .order("id")), so splitting by kind and taking [0] gives
    # exactly the next row _run_once() would pick from that table on the
    # next run - one company + one person per run, same as the bot itself.
    queued_company = [item for item in queued if item["kind"] == "company"]
    queued_person = [item for item in queued if item["kind"] == "person"]
    drafts_company = [item for item in drafts if item["kind"] == "company"]
    drafts_person = [item for item in drafts if item["kind"] == "person"]

    _pipeline_cache = {
        "drafts": drafts, "queued": queued, "waiting": waiting,
        "rejected": rejected, "inbox": inbox, "done": done, "attention": attention,
        "next_company": queued_company[0] if queued_company else None,
        "next_person": queued_person[0] if queued_person else None,
        "queued_company_count": len(queued_company),
        "queued_person_count": len(queued_person),
        "drafts_company": drafts_company,
        "drafts_person": drafts_person,
    }
    return _pipeline_cache


@app.route("/")
def index():
    html = render_template_string(PAGE, logo_b64=LOGO_B64, build_tag=BUILD_TAG)
    resp = app.make_response(html)
    # Never let the browser serve a cached copy of this page - a stale tab
    # showing old markup/JS is a common, very confusing false alarm ("I
    # updated the file but nothing changed") that has nothing to do with
    # whether the file on disk actually changed.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/status")
def api_status():
    pipe = get_pipeline()
    with state_lock:
        s = dict(scheduler_state)
    return jsonify({
        "next_run_at": s["next_run_at"],
        "seconds_left": max(0, int(s["next_run_at"] - time.time())),
        "running_now": s["running_now"],
        "progress": s["progress"],
        "last_run_at": s["last_run_at"],
        "last_run_summary": s["last_run_summary"],
        "last_run_id": s["last_run_id"],
        "last_run_events": s["last_run_events"],
        "batch_size": bot.BATCH_SIZE,
        "review_mode": bot.REVIEW_MODE,
        "drafts": pipe["drafts"],
        "drafts_company": pipe["drafts_company"],
        "drafts_person": pipe["drafts_person"],
        "queued": pipe["queued"],
        "next_company": pipe["next_company"],
        "next_person": pipe["next_person"],
        "queued_company_count": pipe["queued_company_count"],
        "queued_person_count": pipe["queued_person_count"],
        "waiting": pipe["waiting"],
        "inbox": pipe["inbox"],
        "rejected": pipe["rejected"],
        "done": pipe["done"][-50:],  # keep it reasonable, the box scrolls anyway
        "attention": pipe["attention"],
        # Plain counts for the overview stat tiles / donut - computed from
        # the FULL lists above (before the "done" slice above trims it for
        # the table), so these never undercount once a run has been going
        # a while.
        "queued_count": pipe["queued_company_count"] + pipe["queued_person_count"],
        "drafts_count": len(pipe["drafts"]),
        "waiting_count": len(pipe["waiting"]),
        "attention_count": len(pipe["attention"]),
        "rejected_count": len(pipe["rejected"]),
        "booked_count": sum(1 for it in pipe["done"] if it["status"] == "booked"),
        "dead_count": sum(1 for it in pipe["done"] if it["status"] == "dead"),
    })


@app.route("/api/activity-series")
def api_activity_series():
    # ?days=N to widen/narrow the window - defaults to 14 days, matching
    # the dashboard's activity chart.
    days = request.args.get("days", 14, type=int)
    days = max(1, min(days, 90))
    return jsonify({"series": bot.daily_activity_series(days=days)})


@app.route("/api/approve", methods=["POST"])
def api_approve():
    email_addr = request.json.get("email")
    with bot.LEADS_LOCK:
        sent = bot._approve_pending(only_email=email_addr)
    return jsonify({"sent": sent})


@app.route("/api/reject", methods=["POST"])
def api_reject():
    email_addr = request.json.get("email")
    with bot.LEADS_LOCK:
        ok = bot._reject_draft(email_addr)
    return jsonify({"ok": ok})


@app.route("/api/dismiss-attention", methods=["POST"])
def api_dismiss_attention():
    email_addr = request.json.get("email")
    with bot.LEADS_LOCK:
        ok = bot._dismiss_attention(email_addr)
    return jsonify({"ok": ok})


@app.route("/api/run-now", methods=["POST"])
def api_run_now():
    threading.Thread(target=do_scan, args=("manual",), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/no-persona-file")
def api_no_persona_file():
    # Served as plain text (not as_attachment) so it just opens in a
    # browser tab - small, readable, closeable with one click, nothing
    # like the full-screen end-of-scan popup these entries used to show up
    # in. If nothing's been flagged yet the file may not exist - that's
    # fine, just show an empty/friendly placeholder instead of a 404.
    path = bot.NO_PERSONA_LOG_FILE
    if not path.exists():
        return ("# No persona picks needed\n\nNothing's been flagged yet - "
                "this file fills in as leads come up that no persona in "
                "personal_email_templete.txt plausibly fits.\n"), 200, {"Content-Type": "text/plain; charset=utf-8"}
    return send_file(str(path), mimetype="text/plain")


@app.route("/api/daily-recap")
def api_daily_recap():
    # ?date=YYYY-MM-DD to pull a past day's recap instead of today's - not
    # exposed in the UI yet, but handy to hit directly in a browser tab.
    date_str = request.args.get("date") or None
    try:
        pdf_path = bot.generate_daily_recap_pdf(date_str=date_str)
    except SystemExit as e:
        # generate_daily_recap_pdf sys.exit()s with a friendly message if
        # reportlab isn't installed - surface that as a real error instead
        # of killing the whole dashboard process.
        return jsonify({"error": str(e)}), 500
    return send_file(str(pdf_path), as_attachment=True, mimetype="application/pdf")


PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600&display=swap');

  /* Dark "control room" theme (Spikee-style reference) - near-black
     flat panels, thin hairline borders instead of shadows, mono
     uppercase labels, and one mint-green accent carrying every number/
     action that matters. Every color everywhere else in this file is a
     var(...) or color-mix(...) against these tokens, so this block is
     the ONE place the whole look was repainted from - nothing below
     hardcodes a hex color. */
  :root {
    --accent-1: #8FD9AE;
    --accent-2: #B9EAC9;
    --accent-grad: linear-gradient(135deg, #8FD9AE 0%, #6FC79A 100%);
    --cyan: #7FD8C8;
    --good: #8FD9AE;
    --danger: #D98673;
    --warn: #E0B975;
    --violet: #8FB8D9;

    --ink: #ECEFEC;
    --ink-on-accent: #0A0C0A;
    --ink-soft: #9AA39C;
    --ink-faint: #666B64;

    /* Flat near-black base - the "control room" look from the reference:
       no lavender/blue wash, just dark panels with one mint accent. */
    --bg: #0A0C0A;
    --paper: #0F120F;
    --paper-soft: #161A16;
    --paper-raised: #12150F;
    --line: rgba(255,255,255,0.09);
    --line-soft: rgba(255,255,255,0.045);

    --radius-lg: 10px;
    --radius-md: 8px;
    --radius-sm: 6px;

    /* Flat cards, no glow - a thin border carries the edge instead of a
       drop shadow, matching the reference's flat dark panels. */
    --shadow-card: none;
    --shadow-card-hover: 0 0 0 1px var(--line-soft);

    --font-sketch: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  .sketch-font, h1, h2, h3, button, .nav-item, .stat-label, .timer-label,
  .banner-eyebrow, .legend-label, .list-group-label, th, .badge,
  .next-up-chip-label, .chart-tooltip .ct-date {
    font-family: var(--font-sketch);
    letter-spacing: -0.01em;
  }
  .banner-eyebrow, .list-group-label, .timer-label, .next-up-chip-label,
  section.panel h2, th, .stat-label, .legend-label {
    font-family: "JetBrains Mono", "SF Mono", monospace !important;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  section.panel h2 .badge, section.panel h2 .h2-icon { text-transform: none; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  html { -webkit-font-smoothing: antialiased; }
  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    /* Flat near-black base with a faint top vignette - the reference's
       dark control-room feel, no lavender/blue wash. */
    background-image: radial-gradient(1200px 700px at 50% -20%, rgba(255,255,255,0.045), transparent 60%);
    background-color: var(--bg);
    background-attachment: fixed;
    color: var(--ink);
    margin: 0;
    padding: 0;
    letter-spacing: -0.01em;
    height: 100vh;
    overflow: hidden;
  }

  /* Sidebar + scrolling main content, the way the rest of this redesign
     wants a fixed nav rail on the left with everything else (banner, stat
     tiles, charts, the progress bar, the drafts/attention panels) able to
     grow and scroll as one column on the right - instead of the old
     single fixed-height "everything must fit on one screen" layout. */
  .app-shell { display:flex; height: 100vh; }

  .sidebar {
    width: 216px; flex-shrink: 0; height: 100vh;
    background: var(--bg);
    border-right: 1px solid var(--line);
    display: flex; flex-direction: column;
    padding: 22px 14px 16px;
  }
  .sidebar-brand { display:flex; align-items:center; justify-content:flex-start; padding: 14px 10px 22px; }
  .sidebar-brand img { height: auto; width: 100%; max-width: 128px; display:block; }
  .sidebar-nav { display:flex; flex-direction:column; gap: 3px; flex: 1 1 auto; }
  .nav-item {
    display:flex; align-items:center; gap: 11px; padding: 10px 12px;
    border-radius: var(--radius-sm); color: var(--ink-soft); text-decoration:none;
    font-size: 13px; font-weight: 600; letter-spacing: -0.005em;
    transition: background 0.12s ease, color 0.12s ease; cursor: pointer;
    border: none; background: none; width: 100%; text-align:left;
    font-family: inherit;
  }
  .nav-item svg { flex-shrink: 0; opacity: 0.85; }
  .nav-item:hover { background: var(--paper-soft); color: var(--ink); }
  .nav-item.active { background: transparent; color: var(--ink); font-weight: 700; box-shadow: inset 2px 0 0 var(--accent-1); }
  .nav-item.active svg { opacity: 1; color: var(--accent-1); }
  .nav-item .nav-badge {
    margin-left: auto; background: var(--warn); color: var(--ink); font-size: 10.5px;
    font-weight: 800; border-radius: 999px; padding: 1px 7px; min-width: 18px; text-align:center;
  }
  .sidebar-nav { position: relative; }
  .sidebar-nav::before {
    content: ""; position: absolute; left: 25px; top: 20px; bottom: 20px;
    width: 1px; background: var(--line);
  }
  .nav-num {
    font-family: "JetBrains Mono", monospace; font-size: 10px; color: var(--ink-faint);
    width: 16px; flex-shrink: 0; text-align: left;
  }
  .nav-diamond {
    width: 7px; height: 7px; flex-shrink: 0; background: var(--paper);
    border: 1.5px solid var(--ink-faint); transform: rotate(45deg);
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .nav-item:hover .nav-diamond { border-color: var(--accent-1); }
  .nav-item.active .nav-diamond { background: var(--accent-1); border-color: var(--accent-1); }
  .nav-label { flex: 1 1 auto; }
  .topbar {
    display: flex; align-items: center; gap: 18px;
    padding: 0 0 16px; margin-bottom: 20px; border-bottom: 1px solid var(--line);
    font-family: "JetBrains Mono", monospace; font-size: 12px;
  }
  .topbar-title { font-weight: 700; color: var(--ink); }
  .topbar-crumb { color: var(--ink-faint); display:flex; align-items:center; gap:8px; }
  .topbar-search { color: var(--ink-faint); flex: 1 1 auto; }
  .topbar-right {
    color: var(--ink-faint); border: 1px solid var(--line); border-radius: 4px;
    padding: 3px 9px; letter-spacing: 0.08em; flex-shrink: 0;
  }
  .hero-row { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; margin-bottom: 18px; flex-wrap: wrap; }
  .hero-eyebrow {
    font-family: "JetBrains Mono", monospace; font-size: 11.5px; color: var(--ink-faint);
    text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 10px;
  }
  .hero-title { font-size: 32px; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 10px; color: var(--ink); max-width: 760px; }
  .hero-meta-row { display:flex; gap: 28px; margin-top: 16px; }
  .pipeline-panel { margin-bottom: 18px; }
  .pipeline-row { position: relative; display:flex; padding: 28px 24px 22px; }
  .pipeline-row::before { content:""; position:absolute; left:24px; right:24px; top:72px; height:1px; background: var(--line); }
  .pipeline-stage { flex: 1 1 0; min-width:0; display:flex; flex-direction:column; align-items:center; position:relative; z-index:1; }
  .pipeline-num { font-family:"JetBrains Mono", monospace; font-size: 27px; font-weight:800; color: var(--ink); line-height:1; margin-bottom: 15px; font-variant-numeric: tabular-nums; }
  .pipeline-node { width: 9px; height: 9px; background: var(--paper); border: 1.5px solid var(--ink-faint); transform: rotate(45deg); }
  .pipeline-stage.pipeline-current .pipeline-node { background: var(--accent-1); border-color: var(--accent-1); }
  .pipeline-label { font-family:"JetBrains Mono", monospace; font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.07em; margin-top: 12px; }

  .sidebar-foot {
    display:flex; align-items:center; gap: 8px; padding: 12px 12px 4px;
    border-top: 1px solid var(--line-soft); margin-top: 8px;
    font-size: 11.5px; color: var(--ink-faint); font-weight: 600;
  }

  .main { flex: 1 1 auto; min-width: 0; height: 100vh; overflow-y: auto; padding: 26px 32px 40px; }
  .main::-webkit-scrollbar { width: 9px; }
  .main::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--ink) 12%, transparent); border-radius: 8px; }
  .main::-webkit-scrollbar-track { background: transparent; }
  .content { max-width: 1360px; margin: 0 auto; }

  .live-dot {
    width: 7px; height: 7px; border-radius: 50%; background: var(--good); flex-shrink: 0;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--good) 18%, transparent);
    animation: live-pulse 2.4s ease-in-out infinite;
  }
  @keyframes live-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
  .sub { color: var(--ink-soft); font-size: 13px; line-height: 1.6; max-width: 640px; }

  /* Welcome banner + the 4 KPI stat tiles beside it - the row this whole
     redesign was asked for. Real numbers only (queued/drafts/attention/
     booked, straight off /api/status), no illustration lifted from
     anywhere - just a soft gradient + the existing accent colors. */
  .banner-row { display:flex; gap: 16px; align-items:stretch; margin-bottom: 18px; flex-wrap: wrap; }
  .banner-card {
    flex: 1 1 420px;
    position: relative; overflow: hidden;
    background:
      radial-gradient(420px 220px at 100% 0%, color-mix(in srgb, var(--accent-1) 14%, transparent), transparent 65%),
      var(--paper-raised);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    padding: 24px 26px;
    box-shadow: var(--shadow-card);
    display:flex; align-items:center; justify-content:space-between; gap: 20px; flex-wrap:wrap;
  }
  .banner-eyebrow { font-size: 12px; font-weight: 700; color: var(--accent-2); text-transform:uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
  .banner-title { font-size: 26px; font-weight: 800; letter-spacing: -0.02em; margin: 0 0 8px; color: var(--ink); }
  .banner-side { display:flex; align-items:center; gap: 16px; flex-wrap: wrap; }
  .banner-meta { min-width: 96px; }
  .banner-actions { display:flex; gap: 8px; }

  .stat-grid {
    flex: 1 1 380px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  }
  .stat-tile {
    background: var(--paper-raised);
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    padding: 14px 16px;
    box-shadow: var(--shadow-card);
    display:flex; align-items:flex-start; gap: 12px;
    transition: box-shadow 0.15s ease, transform 0.15s ease;
  }
  .stat-tile:hover { box-shadow: var(--shadow-card-hover); transform: translateY(-1px); }
  .stat-icon {
    width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
    display:flex; align-items:center; justify-content:center;
  }
  .stat-blue .stat-icon { background: color-mix(in srgb, var(--accent-1) 16%, transparent); color: var(--accent-1); }
  .stat-violet .stat-icon { background: color-mix(in srgb, var(--violet) 16%, transparent); color: var(--violet); }
  .stat-amber .stat-icon { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
  .stat-green .stat-icon { background: color-mix(in srgb, var(--good) 16%, transparent); color: var(--good); }
  .stat-num { font-size: 21px; font-weight: 800; letter-spacing: -0.01em; line-height: 1.2; color: var(--ink); }
  .stat-label { font-size: 11.5px; color: var(--ink-faint); font-weight: 600; margin-top: 1px; }

  .timer { font-family: "JetBrains Mono", "SF Mono", monospace; font-size: 25px; font-weight: 600; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; line-height:1.2; color: var(--ink); }
  .timer-label { font-size: 10.5px; color: var(--ink-faint); font-weight: 600; text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 5px; }

  /* The two overview charts - a real 14-day sent/booked history on the
     left (from activity_log.md, via /api/activity-series) and a donut of
     the current pipeline split on the right (from the same counts already
     driving the stat tiles above). */
  .chart-row { display:flex; gap: 16px; margin-bottom: 18px; flex-wrap: wrap; }
  .chart-card { flex: 2 1 480px; min-width: 320px; }
  .chart-body { position: relative; padding: 6px 20px 18px; flex: 1 1 auto; min-height: 200px; }
  .chart-body svg { width: 100%; height: 200px; display:block; overflow: visible; }
  .chart-tooltip {
    position: absolute; pointer-events:none; background: var(--paper-raised);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    padding: 7px 11px; font-size: 11.5px; color: var(--ink); box-shadow: var(--shadow-card);
    transform: translate(-50%, -115%); white-space: nowrap; z-index: 5;
  }
  .chart-tooltip .ct-date { color: var(--ink-faint); font-size: 10.5px; margin-bottom: 2px; }
  .chart-crosshair { stroke: var(--line); stroke-width: 1; }


  button {
    background: var(--accent-1);
    color: var(--ink-on-accent);
    border: none;
    border-radius: 8px;
    padding: 13px 26px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: -0.01em;
    transition: transform 0.15s ease, filter 0.15s ease, background 0.15s ease;
    box-shadow: none;
  }
  button:hover { background: var(--accent-2); transform: translateY(-1px); box-shadow: none; filter: none; }
  button:active { transform: translateY(0); }
  button.reject { background: transparent; color: var(--ink-soft); box-shadow: none; border: 1px solid var(--line); }
  button.reject:hover { background: var(--paper-soft); border-color: color-mix(in srgb, var(--ink) 16%, transparent); color: var(--ink); transform: translateY(-1px); box-shadow:none; filter:none; }
  button:disabled { opacity:0.4; cursor:default; transform:none; }
  .btn-row { display:flex; gap:10px; }
  .btn-row button { padding: 10px 20px; font-size: 13px; }
  button.table-btn { padding: 6px 14px; font-size: 12px; white-space: nowrap; }

  /* Always on screen - a permanent fixture, not something that pops in and
     out. Idle: empty pill sitting there. Running: fills smoothly left to
     right. Finished: holds full for a moment, then drains back to empty
     (never disappears) so the bar is always exactly where you expect it. */
  .progress-wrap { margin-bottom: 20px; flex-shrink: 0; }
  .progress-label {
    font-size: 12.5px; color: var(--ink-soft); margin-bottom: 10px;
    display:flex; justify-content:space-between; gap:12px;
    font-weight: 500; letter-spacing: -0.01em;
  }
  .progress-label span:last-child {
    color: var(--ink); flex-shrink:0; font-variant-numeric: tabular-nums;
    font-weight: 600; font-feature-settings: "tnum";
  }
  /* Segmented capsule bar with round end-caps, matching the reference:
     one continuous fill glides underneath in real time (unchanged JS,
     same #progress-fill width/indeterminate logic as before) - a mask on
     the track just cuts it into four rounded chunks with gaps, the same
     visual trick a native segmented control uses. */
  .progress-bar-row { display:flex; align-items:center; gap: 10px; }
  .progress-track {
    position: relative; height: 10px; border-radius: 999px; flex: 1;
    background: var(--paper-soft);
    box-shadow: inset 0 1px 2px color-mix(in srgb, var(--ink) 8%, transparent), inset 0 0 0 1px var(--line-soft);
    overflow: hidden;
    -webkit-mask-image: repeating-linear-gradient(90deg, #000 0, #000 calc(25% - 5px), transparent calc(25% - 5px), transparent 25%);
    mask-image: repeating-linear-gradient(90deg, #000 0, #000 calc(25% - 5px), transparent calc(25% - 5px), transparent 25%);
  }
  .progress-fill {
    position: absolute; top:0; left:0; height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, var(--violet) 0%, var(--accent-1) 100%);
    width: 0%;
    transition: width 0.7s cubic-bezier(0.16, 1, 0.3, 1);
    box-shadow: 0 0 12px color-mix(in srgb, var(--accent-1) 35%, transparent);
  }
  /* Round pucks bookending the bar - neutral at rest, light up in the
     accent gradient the moment a run is actively animating, and the end
     one turns solid green with a checkmark once the run has finished. */
  .progress-cap {
    width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0;
    display:flex; align-items:center; justify-content:center;
    background: var(--paper-soft);
    box-shadow: inset 0 0 0 1px var(--line);
    transition: background 0.4s ease, box-shadow 0.4s ease, transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
  }
  .progress-cap svg { width: 11px; height: 11px; display:block; }
  .progress-cap-end svg path {
    stroke: var(--ink-faint); stroke-width: 2.4; fill: none;
    stroke-linecap: round; stroke-linejoin: round;
    transition: stroke 0.4s ease;
  }
  .progress-cap-start::after {
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: color-mix(in srgb, var(--ink) 22%, transparent);
    transition: background 0.4s ease, transform 0.3s ease;
  }
  .progress-wrap[data-animate="1"] .progress-cap-start {
    background: linear-gradient(135deg, var(--violet) 0%, var(--accent-1) 100%);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-1) 18%, transparent);
  }
  .progress-wrap[data-animate="1"] .progress-cap-start::after {
    background: #fff; transform: scale(1.15);
  }
  .progress-wrap.finished .progress-cap-end {
    background: linear-gradient(135deg, var(--good) 0%, var(--cyan) 100%);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--good) 20%, transparent);
    transform: scale(1.08);
  }
  .progress-wrap.finished .progress-cap-end svg path { stroke: #fff; }
  /* a soft traveling highlight riding on top of the fill, like the sheen
     on iOS/macOS progress indicators - subtle, not a loading-bar gimmick */
  .progress-fill::after {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.35) 50%, transparent 100%);
    background-size: 60px 100%; background-repeat: no-repeat;
    background-position: -60px 0;
    opacity: 0; animation: progress-sheen 2.2s ease-in-out infinite;
  }
  @keyframes progress-sheen {
    0% { background-position: -60px 0; opacity: 0; }
    15% { opacity: 0.9; }
    55% { opacity: 0; }
    100% { background-position: 340px 0; opacity: 0; }
  }
  .progress-fill.emptying { transition: width 0.8s cubic-bezier(0.16, 1, 0.3, 1); }
  .progress-fill.indeterminate {
    width: 28%; transition: none; animation: progress-slide 1.35s cubic-bezier(0.45, 0, 0.55, 1) infinite;
  }
  .progress-fill.indeterminate::after { display: none; }
  @keyframes progress-slide {
    0% { left: -28%; }
    100% { left: 100%; }
  }
  .dots { display:inline-block; width: 1.2em; text-align:left; }

  .step-dots {
    display:flex; align-items:center; gap: 18px; margin-top: 13px;
    flex-wrap: wrap;
  }
  .step-dots .step {
    display:flex; align-items:center; gap: 6px; font-size: 11.5px;
    color: var(--ink-faint); font-weight: 500; letter-spacing: -0.01em;
    transition: color 0.3s ease;
  }
  .step-dots .step .dot {
    width: 5px; height: 5px; border-radius: 50%;
    background: color-mix(in srgb, var(--ink) 16%, transparent);
    flex-shrink:0; transition: background 0.3s ease, box-shadow 0.3s ease, transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .step-dots .step.done .dot { background: var(--cyan); }
  .step-dots .step.active { color: var(--ink); }
  .step-dots .step.active .dot {
    background: var(--accent-1); transform: scale(1.5);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent-1) 16%, transparent);
  }

  /* Two-pane, email-client layout: a narrow list of drafts on the left,
     one big reading pane on the right that shows the SELECTED draft in
     full - no clipping, no "show more" - since the entire point of this
     layout is to make one email at a time genuinely easy to read. */
  .email-layout {
    display:grid;
    grid-template-columns: 340px 1fr;
    gap: 16px;
    /* .main scrolls the whole page now (sidebar layout, see .app-shell/
       .main above), so this section no longer stretches to fill a fixed
       viewport - it gets its own bounded height instead, and the two
       panels inside it (draft list / reading pane) keep scrolling
       independently within that, same as before. */
    height: 640px;
    min-height: 0;
  }

  section.panel {
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-card);
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  section.panel h2 {
    font-size: 13.5px; font-weight: 700; color: var(--ink); margin: 0;
    padding: 17px 20px 13px;
    display:flex; align-items:center; gap: 9px; justify-content:space-between;
    letter-spacing: -0.005em;
    flex-shrink: 0;
    border-bottom: 1px solid var(--line-soft);
  }
  section.panel h2 .h2-title { display:flex; align-items:center; gap: 9px; }
  section.panel h2 .h2-icon { flex-shrink: 0; opacity: 0.75; display:flex; }
  .scrollbox { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 12px 16px 16px; }
  .scrollbox::-webkit-scrollbar { width: 7px; }
  .scrollbox::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--ink) 12%, transparent); border-radius: 8px; }
  .scrollbox::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--ink) 20%, transparent); }
  .scrollbox::-webkit-scrollbar-track { background: transparent; }

  /* The left-hand list: compact rows, one per draft, click to open in the
     reading pane. Color-coded left edge (blue=company, violet=person) so
     you can tell the two apart without reading the group label. */
  .list-group-label {
    font-size: 10.5px; font-weight: 700; color: var(--ink-faint); text-transform: uppercase;
    letter-spacing: 0.06em; padding: 10px 4px 6px; display:flex; align-items:center; gap:7px;
  }
  .list-group-label .badge { padding: 1px 8px; font-size: 10px; }
  .draft-row {
    background: var(--paper-soft);
    border-radius: var(--radius-sm);
    padding: 10px 12px 10px 13px;
    margin-bottom: 6px;
    border: 1px solid var(--line-soft);
    border-left: 3px solid var(--accent-1);
    cursor: pointer;
    transition: background 0.12s ease, border-color 0.12s ease;
  }
  .draft-row.kind-person { border-left-color: var(--violet); }
  .draft-row:hover { background: var(--paper-raised); }
  .draft-row.active { background: var(--paper-raised); box-shadow: 0 0 0 1px var(--accent-1) inset; }
  .draft-row.kind-person.active { box-shadow: 0 0 0 1px var(--violet) inset; }
  .draft-row .row-name { font-weight: 700; font-size: 13px; color: var(--ink); }
  .draft-row .row-sub { font-size: 11px; color: var(--ink-faint); margin-top: 1px; }
  .draft-row .row-subject {
    font-size: 12px; color: var(--ink-soft); margin-top: 4px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  /* The compact "what's coming next" strip that used to be its own panel -
     folded into the top of the sidebar so almost all the vertical space
     on screen goes to actually reading emails. */

  /* The reading pane - the whole point of this redesign. Full height,
     generous type, the body never gets clipped: it's either fully on
     screen or you scroll this ONE box (reading-body-wrap), never a tiny
     clipped card. */
  .reading-panel { padding: 0; }
  /* #reading-pane is a plain <div> that JS replaces the innerHTML of - it
     has to be a flex column itself (not just its parent section.panel)
     or the header/subject/body-wrap below lose their flex sizing, the
     body never gets a bounded height to scroll within, and it just
     silently overflows and gets clipped by section.panel's
     "overflow: hidden" instead - which is exactly what "can't scroll or
     do anything" was: the content was there, just cut off with no
     scrollbar and the buttons potentially out of the clipped area. */
  #reading-pane {
    display:flex; flex-direction:column; flex:1 1 auto; min-height:0; height:100%;
  }
  .reading-empty {
    flex: 1 1 auto; display:flex; align-items:center; justify-content:center;
    color: var(--ink-faint); font-size: 14px; text-align:center; padding: 40px;
  }
  /* Outlook-ish message chrome: a slim ribbon-style toolbar up top, then
     the subject large, then a From/To meta row with a colored initials
     avatar, then a divider before the body - the classic reading-pane
     layout, applied to a drafted (not-yet-sent) email. */
  .ol-toolbar {
    display:flex; gap: 6px; padding: 10px 16px; border-bottom: 1px solid var(--line-soft);
    background: var(--paper-soft); flex-shrink: 0;
  }
  .ol-toolbar button {
    display:flex; align-items:center; gap: 6px;
    background: var(--paper-raised); color: var(--ink);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    padding: 7px 13px; font-size: 12.5px; font-weight: 700;
    box-shadow: none;
  }
  .ol-toolbar button:hover { background: color-mix(in srgb, var(--accent-1) 12%, var(--paper-raised)); transform:none; filter:none; box-shadow:none; }
  .ol-toolbar button.reject:hover { background: color-mix(in srgb, var(--danger) 14%, var(--paper-raised)); }
  .ol-subject { padding: 18px 28px 10px; font-size: 19px; font-weight: 700; color: var(--ink); flex-shrink:0; letter-spacing: -0.005em; }
  .ol-meta-row {
    padding: 0 28px 16px; display:flex; align-items:center; gap: 12px; flex-shrink: 0;
    border-bottom: 1px solid var(--line-soft);
  }
  .ol-avatar {
    width: 38px; height: 38px; border-radius: 50%; flex-shrink: 0;
    display:flex; align-items:center; justify-content:center;
    color: #fff; font-weight: 700; font-size: 14px; letter-spacing: 0.02em;
  }
  .ol-meta-text { min-width: 0; flex: 1 1 auto; }
  .ol-from { font-size: 14px; font-weight: 700; color: var(--ink); }
  .ol-from .ol-email { font-weight: 500; color: var(--ink-faint); }
  .ol-to { font-size: 12px; color: var(--ink-faint); margin-top: 1px; }
  .ol-kind-pill { flex-shrink: 0; }
  .reading-body-wrap { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 18px 28px 28px; }
  .reading-body-wrap::-webkit-scrollbar { width: 8px; }
  .reading-body-wrap::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--ink) 12%, transparent); border-radius: 8px; }
  .reading-body-wrap::-webkit-scrollbar-track { background: transparent; }
  /* Hebrew (or any RTL-script) subject/body flips to right-to-left and
     right alignment automatically via dir="auto" set in the markup below
     - this rule just makes sure the max-width reading column still reads
     naturally either direction, instead of being pinned to the left. */
  .reading-body { white-space: pre-wrap; font-size: 15.5px; color: var(--ink); line-height: 1.85; max-width: 760px; }
  .actions { display:flex; gap:8px; flex-shrink:0; }

  table { width:100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align:left; padding: 10px 9px; border-bottom: 1px solid var(--line-soft); }
  th {
    color: var(--ink-faint); font-weight: 600; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
    position: sticky; top: 0; background: var(--paper); background: linear-gradient(180deg, var(--paper-raised) 0%, var(--paper) 100%);
  }
  tbody tr { transition: background 0.12s ease; }
  tbody tr:hover { background: color-mix(in srgb, var(--ink) 2.5%, transparent); }
  tbody tr:last-child td { border-bottom: none; }

  .status-pill {
    display:inline-flex; align-items:center; gap: 5px; padding: 2px 9px; border-radius: 4px; font-size: 10px; font-weight: 700;
    background: transparent; border: 1px solid var(--line); color: var(--ink-soft); letter-spacing: 0.04em;
    font-family: "JetBrains Mono", monospace; text-transform: uppercase;
  }
  .status-pill::before { display: none; }
  .status-pill.s-booked { border-color: color-mix(in srgb, var(--good) 55%, transparent); color: var(--good); }
  .status-pill.s-dead { border-color: color-mix(in srgb, var(--danger) 55%, transparent); color: var(--danger); }
  .status-pill.s-needs_attention { border-color: color-mix(in srgb, var(--warn) 55%, transparent); color: var(--warn); }
  .status-pill.s-awaiting_referral { border-color: color-mix(in srgb, var(--violet) 55%, transparent); color: var(--violet); }
  .status-pill.kind-company { border-color: color-mix(in srgb, var(--accent-1) 55%, transparent); color: var(--accent-1); }
  .status-pill.kind-person { border-color: color-mix(in srgb, var(--violet) 55%, transparent); color: var(--violet); }

  .panel.panel-attention {
    border: 1px solid color-mix(in srgb, var(--warn) 40%, transparent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 8%, transparent), var(--shadow-card);
    max-height: 220px; margin-bottom: 16px; flex: 0 0 auto;
  }
  .panel.panel-attention h2 { color: var(--warn); }

  .empty { color: var(--ink-faint); font-size: 13px; padding: 20px 0; text-align:center; }

  .badge {
    background: transparent; color: var(--ink); border: 1px solid var(--line); border-radius: 4px;
    padding: 1px 8px; font-size: 11px; font-weight: 700; min-width: 20px; text-align:center;
  }
  .badge.zero { color: var(--ink-faint); border-color: var(--line-soft); }
  .row-email { color: var(--ink-soft); }

  .build-tag {
    position: fixed; bottom: 10px; right: 14px; font-size: 10px; color: var(--ink-faint);
    font-family: "JetBrains Mono", monospace;
    opacity: 0.4; letter-spacing: 0.02em; pointer-events: none; z-index: 999;
  }

  .modal-overlay {
    position: fixed; inset: 0; background: rgba(5,6,10,0);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000; padding: 20px;
    opacity: 0; pointer-events: none;
    transition: background 0.25s ease, opacity 0.25s ease;
  }
  .modal-overlay.show { background: rgba(5,6,10,0.6); backdrop-filter: blur(6px); opacity: 1; pointer-events: auto; }
  .modal-box {
    background: linear-gradient(180deg, var(--paper-raised) 0%, var(--paper) 220px);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    max-width: 560px; width: 100%; max-height: 80vh;
    display: flex; flex-direction: column;
    box-shadow: var(--shadow-card-hover);
    opacity: 0; transform: translateY(6px) scale(0.98);
    transition: opacity 0.25s ease, transform 0.25s ease;
  }
  .modal-overlay.show .modal-box { opacity: 1; transform: translateY(0) scale(1); }
  .modal-box h3 { margin: 0; padding: 22px 24px 2px; font-size: 16.5px; letter-spacing: -0.01em; color: var(--ink); }
  .modal-summary { padding: 6px 24px 16px; color: var(--ink-soft); font-size: 13px; }
  .modal-list { overflow-y: auto; padding: 0 24px 8px; flex: 1 1 auto; }
  .modal-item { display: flex; gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--line-soft); font-size: 13px; }
  .modal-item:last-child { border-bottom: none; }
  .modal-item .mi-icon { flex-shrink: 0; font-size: 16px; line-height: 1.4; }
  .modal-item .mi-text { flex: 1 1 auto; min-width: 0; }
  .modal-item .mi-kind { font-weight: 700; color: var(--ink); }
  .modal-item .mi-who { color: var(--ink-soft); margin-top: 1px; word-break: break-word; }
  .modal-empty { padding: 8px 0 20px; color: var(--ink-faint); font-size: 13px; }
  .modal-footer { padding: 14px 24px 22px; text-align: right; flex-shrink: 0; }
</style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar">
    <div class="sidebar-brand">
      <img src="data:image/png;base64,{{ logo_b64 }}" alt="Spikke">
    </div>
    <nav class="sidebar-nav">
      <a href="#dashboard-top" class="nav-item active" data-nav="top">
        <span class="nav-num">00</span><span class="nav-diamond"></span>
        <span class="nav-label">Overview</span>
      </a>
      <a href="#email-layout" class="nav-item" data-nav="drafts">
        <span class="nav-num">01</span><span class="nav-diamond"></span>
        <span class="nav-label">Drafts</span>
        <span class="nav-badge" id="nav-draft-badge" style="display:none;">0</span>
      </a>
      <a href="#attention-panel" class="nav-item" data-nav="attention">
        <span class="nav-num">02</span><span class="nav-diamond"></span>
        <span class="nav-label">Attention</span>
        <span class="nav-badge" id="nav-attention-badge" style="display:none;">0</span>
      </a>
      <a href="/api/no-persona-file" target="_blank" class="nav-item"
         title="Leads no persona plausibly matched - opens as a plain file, not a popup">
        <span class="nav-num">03</span><span class="nav-diamond"></span>
        <span class="nav-label">Persona picks</span>
      </a>
      <button type="button" class="nav-item" id="recap-btn" onclick="downloadRecap()"
              title="Download a PDF summary of today's activity">
        <span class="nav-num">04</span><span class="nav-diamond"></span>
        <span class="nav-label">Daily recap</span>
      </button>
    </nav>
    <div class="sidebar-foot">
      <span class="live-dot" title="Bot is running"></span>
      <span id="sidebar-live-text">idle</span>
    </div>
  </aside>

  <main class="main">
    <div class="content" id="dashboard-top">

      <div class="topbar">
        <div class="topbar-crumb"><span class="topbar-title">Overview</span></div>
        <div class="topbar-search">&gt; search leads or companies</div>
        <div class="topbar-right" id="topbar-live">LIVE</div>
      </div>

      <div class="hero-row">
        <div class="hero-text">
          <div class="hero-eyebrow">Overview</div>
          <h1 class="hero-title">Itay, here's what's running.</h1>
          <div class="sub" id="sub">loading...</div>
          <div class="hero-meta-row">
            <div class="banner-meta">
              <div class="timer-label">Next scan in</div>
              <div class="timer" id="timer">--:--</div>
            </div>
            <div class="banner-meta">
              <div class="timer-label">Batch size</div>
              <div class="timer" id="batchsize" style="font-size:22px;">-</div>
            </div>
          </div>
        </div>
        <div class="banner-actions">
          <button id="run-btn" onclick="runNow()">Run now</button>
        </div>
      </div>

      <section class="panel pipeline-panel">
        <h2><span class="h2-title">Pipeline</span><span class="live-dot"></span></h2>
        <div class="pipeline-row">
          <div class="pipeline-stage">
            <div class="pipeline-num" id="stat-queued">0</div>
            <div class="pipeline-node"></div>
            <div class="pipeline-label">Queued</div>
          </div>
          <div class="pipeline-stage">
            <div class="pipeline-num" id="stat-drafts">0</div>
            <div class="pipeline-node"></div>
            <div class="pipeline-label">Drafted</div>
          </div>
          <div class="pipeline-stage">
            <div class="pipeline-num" id="stat-attention">0</div>
            <div class="pipeline-node"></div>
            <div class="pipeline-label">Attention</div>
          </div>
          <div class="pipeline-stage pipeline-current">
            <div class="pipeline-num" id="stat-booked">0</div>
            <div class="pipeline-node"></div>
            <div class="pipeline-label">Meetings</div>
          </div>
        </div>
      </section>

      <div class="progress-wrap" id="progress-wrap" data-animate="0">
        <div class="progress-label"><span><span id="progress-text-base">Idle</span><span id="progress-dots" class="dots"></span></span><span id="progress-pct"></span></div>
        <div class="progress-bar-row">
          <div class="progress-cap progress-cap-start"></div>
          <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
          <div class="progress-cap progress-cap-end" id="progress-cap-end">
            <svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg>
          </div>
        </div>
        <div class="step-dots" id="step-dots"></div>
      </div>

      <div class="chart-row">
        <section class="panel chart-card" style="flex: 1 1 100%;">
          <h2><span class="h2-title">Emails sent <span style="color:var(--ink-faint); font-weight:600;">— last 14 days</span></span></h2>
          <div class="chart-body">
            <svg id="area-chart" viewBox="0 0 600 200" preserveAspectRatio="none"></svg>
            <div class="chart-tooltip" id="area-tooltip" style="display:none;"></div>
          </div>
        </section>
      </div>

      <section class="panel panel-attention" id="attention-panel" style="display:none;">
        <h2><span class="h2-title"><span class="h2-icon"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>Needs your attention</span> <span class="badge" id="attention-count">0</span></h2>
        <div class="scrollbox">
          <table id="attention-table"><thead><tr><th>Company</th><th>Title</th><th>Email</th><th>Status</th><th>Notes</th><th></th></tr></thead><tbody></tbody></table>
        </div>
      </section>

      <div class="email-layout" id="email-layout">

        <section class="panel">
          <h2><span class="h2-title"><span class="h2-icon"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg></span>Drafts awaiting your approval</span> <span class="badge" id="draft-count">0</span></h2>
          <div class="scrollbox" id="draft-list"></div>
        </section>

        <section class="panel reading-panel">
          <div id="reading-pane"><div class="reading-empty">Select a draft on the left to read it in full.</div></div>
        </section>

      </div>
    </div>
  </main>
</div>
<div class="build-tag">{{ build_tag }}</div>

<div class="modal-overlay" id="run-summary-overlay" onclick="if (event.target === this) closeRunSummary()">
  <div class="modal-box" onmouseenter="if (runSummaryAutoCloseTimer) clearTimeout(runSummaryAutoCloseTimer);" onmouseleave="armRunSummaryAutoClose();">
    <h3>Scan finished</h3>
    <div class="modal-summary" id="run-summary-counts"></div>
    <div class="modal-list" id="run-summary-list"></div>
    <div class="modal-footer"><button onclick="closeRunSummary()">Got it</button></div>
  </div>
</div>

<script>
function fmtTime(sec) {
  if (sec <= 0) return "now...";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ":" + String(s).padStart(2, "0");
}

// escapeHtml (below) is only safe for TEXT content - it relies on
// innerHTML of a text node, which never needs to escape quote characters.
// Anything going into an HTML ATTRIBUTE (data-email="...") needs quotes
// escaped too, or a value containing one breaks the attribute right open.
// This is also why inline onclick="fn(${JSON.stringify(x)})" is never
// safe here: JSON.stringify always wraps its result in double quotes,
// which immediately terminates a double-quoted attribute for every
// single row, not just ones with unusual characters in them.
function escapeAttr(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.innerText = s || "";
  return d.innerHTML;
}

function setBadge(id, n) {
  const el = document.getElementById(id);
  el.innerText = n;
  el.classList.toggle("zero", n === 0);
}

function statusPill(status) {
  const cls = "s-" + (status || "").toLowerCase().replace(/[^a-z_]/g, "");
  return `<span class="status-pill ${cls}">${escapeHtml(status)}</span>`;
}

// Outlook-style avatar for the reading pane - initials + a deterministic
// color from the pastel palette, same idea as any email client's "no
// profile photo" fallback. Works fine on a Hebrew name (or any script) -
// it just takes the first letter(s) of whatever words are there.
function initialsFor(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return [...parts[0]].slice(0, 2).join("").toUpperCase();
  return ([...parts[0]][0] + [...parts[1]][0]).toUpperCase();
}
const AVATAR_PALETTE = ["var(--accent-1)", "var(--violet)", "var(--good)", "var(--warn)", "var(--danger)", "var(--accent-2)"];
function avatarColorFor(name) {
  let h = 0;
  for (const ch of (name || "")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_PALETTE[h % AVATAR_PALETTE.length];
}

function setNavBadge(id, n) {
  const el = document.getElementById(id);
  el.innerText = n;
  el.style.display = n > 0 ? "" : "none";
}

// Sidebar nav is a set of in-page anchors (each target lives inside .main,
// the scrolling element - see .app-shell/.main in the CSS), so the browser
// already scrolls to the right place on its own; this just keeps exactly
// one item marked "active" to match wherever you last clicked.
document.querySelectorAll(".sidebar-nav a.nav-item").forEach(a => {
  a.addEventListener("click", () => {
    document.querySelectorAll(".sidebar-nav a.nav-item").forEach(x => x.classList.remove("active"));
    a.classList.add("active");
  });
});

// ---------------------------------------------------------------------
// Pipeline donut - a real, live breakdown of every lead currently loaded
// (from the exact same counts feeding the stat tiles above), grouped into
// ---------------------------------------------------------------------
// Activity area chart - real per-day sent/booked counts from
// activity_log.md (via /api/activity-series), not a fabricated trend.
// Fetched independently of the fast status poll since this only changes
// once a day at most - loaded once on page open and refreshed every few
// minutes so an overnight scan shows up without a manual reload.
// ---------------------------------------------------------------------
let lastSeries = null;

// A small, deterministic "hand tremor" per index - stable across
// re-renders (the same data always wobbles the same way, it never
// reshuffles on its own), just enough curvature to read as a hand-drawn
// line rather than a ruler-straight one. Only ever bends the curve
// BETWEEN two real data points - the points themselves stay exactly on
// their true value, so the wobble is decorative, never a distortion of
// what the chart is actually showing.
function seededWobble(i, amp) {
  const s = Math.sin(i * 12.9898 + 4.1414) * 43758.5453;
  return ((s - Math.floor(s)) * 2 - 1) * amp;
}

function wobblyPath(points) {
  let d = `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i];
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    // Perpendicular to the segment, so the wobble bows the curve up/down
    // rather than along it.
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;
    const wob = seededWobble(i, 3.2);
    const cx = mx + nx * wob, cy = my + ny * wob;
    d += ` Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
  }
  return d;
}

function drawAreaChart(series) {
  const svg = document.getElementById("area-chart");
  const W = 600, H = 200, padL = 6, padR = 6, padT = 14, padB = 22;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const maxVal = Math.max(1, ...series.map(d => d.sent));
  const step = series.length > 1 ? innerW / (series.length - 1) : 0;
  const points = series.map((d, i) => {
    const x = padL + i * step;
    const y = padT + innerH - (d.sent / maxVal) * innerH;
    return {x, y, d};
  });
  const linePath = wobblyPath(points);
  const areaPath = linePath +
    ` L ${points[points.length - 1].x.toFixed(1)} ${(padT + innerH).toFixed(1)}` +
    ` L ${points[0].x.toFixed(1)} ${(padT + innerH).toFixed(1)} Z`;

  // Sparse x-axis labels (first, middle, last) - a label under every one
  // of 14 points is just noise at this width.
  const labelIdxs = series.length > 1 ? [0, Math.floor((series.length - 1) / 2), series.length - 1] : [0];
  const labels = labelIdxs.map(i => {
    const p = points[i];
    const short = (series[i].date || "").slice(5); // MM-DD
    return `<text x="${p.x.toFixed(1)}" y="${H - 6}" font-size="9.5" fill="var(--ink-faint)" ` +
      `text-anchor="${i === 0 ? "start" : i === series.length - 1 ? "end" : "middle"}">${short}</text>`;
  }).join("");

  const dots = points.map((p, i) =>
    `<circle class="area-pt" data-i="${i}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="8" fill="transparent"/>`
  ).join("");

  svg.innerHTML = `
    <defs>
      <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#5D82C9" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="#5D82C9" stop-opacity="0.03"/>
      </linearGradient>
    </defs>
    <line x1="${padL}" y1="${(padT + innerH).toFixed(1)}" x2="${W - padR}" y2="${(padT + innerH).toFixed(1)}" stroke="var(--line-soft)" stroke-width="1"/>
    <path d="${areaPath}" fill="url(#areaFill)" stroke="none"/>
    <path d="${linePath}" fill="none" stroke="#5D82C9" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" opacity="0.5" transform="translate(1.2 1)"/>
    <path d="${linePath}" fill="none" stroke="#4A6BAD" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}
    ${labels}
  `;

  const tooltip = document.getElementById("area-tooltip");
  svg.querySelectorAll(".area-pt").forEach(el => {
    el.style.cursor = "pointer";
    el.addEventListener("mouseenter", () => {
      const i = +el.dataset.i;
      const p = points[i];
      const rect = svg.getBoundingClientRect();
      const bodyRect = svg.parentElement.getBoundingClientRect();
      const px = rect.left - bodyRect.left + (p.x / W) * rect.width;
      const py = rect.top - bodyRect.top + (p.y / H) * rect.height;
      tooltip.innerHTML = `<div class="ct-date">${escapeHtml(series[i].date)}</div>` +
        `<div><b>${series[i].sent}</b> sent${series[i].booked ? ` · <b>${series[i].booked}</b> booked` : ""}</div>`;
      tooltip.style.left = px + "px";
      tooltip.style.top = py + "px";
      tooltip.style.display = "block";
    });
    el.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  });
}

async function loadActivitySeries() {
  try {
    const res = await fetch("/api/activity-series?days=14");
    if (!res.ok) return;
    const data = await res.json();
    const sig = JSON.stringify(data.series);
    if (sig === lastSeries) return;
    lastSeries = sig;
    drawAreaChart(data.series);
  } catch (err) {
    // Non-fatal - the rest of the dashboard keeps working even if this
    // one fetch fails; it'll just retry on the next interval.
  }
}

// The finer-grained sub-step within whichever lead/reply is currently being
// worked on - what the backend reports as progress.step. These are shown
// as a small dot-stepper under the bar, e.g. "checking liveness -> reading
// website -> writing email", so it's obvious what's actually happening
// right now instead of the bar just sitting there between polls.
const LEADS_STEPS = [
  {key: "liveness", label: "Checking email liveness"},
  {key: "website", label: "Reading their website"},
  {key: "compose", label: "Writing the email"},
  {key: "compose_batch", label: "Writing a batch of emails together"},
  {key: "sending", label: "Sending"},
];
const REPLIES_STEPS = [
  {key: "reading", label: "Reading their reply"},
  {key: "calendar", label: "Checking your calendar"},
  {key: "sending", label: "Sending the reply"},
];

function renderStepDots(stage, step) {
  const seq = stage === "leads" ? LEADS_STEPS : stage === "replies" ? REPLIES_STEPS : null;
  const stepDotsEl = document.getElementById("step-dots");
  if (!seq || !step) {
    stepDotsEl.innerHTML = "";
    return;
  }
  const activeIdx = seq.findIndex(s => s.key === step);
  stepDotsEl.innerHTML = seq.map((s, i) => {
    const cls = i === activeIdx ? "step active" : i < activeIdx ? "step done" : "step";
    return `<div class="${cls}"><div class="dot"></div>${escapeHtml(s.label)}</div>`;
  }).join("");
}

// Turns the raw {stage, current, total, label, step} the backend reports
// into a bar percentage + human text. Leads (drafting new cold emails)
// fills the first half of the bar, replies (handling inbox responses) the
// second half - each determinate once its count is known. The inbox-check
// step in between has no count yet, so it shows as a sliding indeterminate
// bar instead of pretending to be a number. `animate: true` means an
// animated "..." should follow the text (actively working on something);
// false means it's a settled state (nothing to do, or done).
const STARTING_STEP_TEXT = {
  connecting: "Connecting to the AI",
  loading: "Loading leads",
  backfill_research: "Catching up on company research",
  backfill_role: "Catching up on role classification",
  role_classify: "Classifying new leads' roles (one batch call)",
  checking_new: "Checking for new leads",
};

function computeProgress(p) {
  if (!p || p.stage === "idle") return null;
  if (p.stage === "starting") {
    let text = STARTING_STEP_TEXT[p.step] || "Starting scan";
    // The two backfill passes report a real current/total + which
    // company/contact they're on - show that instead of just the bare
    // label, same idea as the "leads" stage's "Drafting 2/5 — Acme" text.
    if ((p.step === "backfill_research" || p.step === "backfill_role" || p.step === "role_classify") && p.total) {
      text = p.label ? `${text} (${p.current}/${p.total}) — ${p.label}`
                     : `${text} (${p.current}/${p.total})`;
    }
    return {pct: 4, text, animate: true};
  }
  if (p.stage === "leads") {
    if (p.total === 0) return {pct: 50, text: "No new leads to contact", animate: false};
    const frac = p.current / p.total;
    const text = p.label ? `Drafting ${p.current}/${p.total} — ${p.label}` : `Drafting ${p.current}/${p.total}`;
    return {pct: 4 + frac * 46, text, animate: true, stage: p.stage, step: p.step};
  }
  if (p.stage === "inbox_check") {
    return {pct: 50, text: "Checking inbox for replies", indeterminate: true, animate: true};
  }
  if (p.stage === "replies") {
    if (p.total === 0) return {pct: 100, text: "No replies to handle", animate: false};
    const frac = p.current / p.total;
    const text = p.label ? `Handling reply ${p.current}/${p.total} — ${p.label}` : `Handling reply ${p.current}/${p.total}`;
    return {pct: 52 + frac * 48, text, animate: true, stage: p.stage, step: p.step};
  }
  if (p.stage === "calendly") {
    return {pct: 99, text: "Checking Calendly for new bookings", indeterminate: true, animate: true};
  }
  if (p.stage === "learning") {
    return {pct: 99.5, text: "Learning from recent outcomes", indeterminate: true, animate: true};
  }
  if (p.stage === "done") {
    return {pct: 100, text: "Done", animate: false};
  }
  return null;
}

// Turns a raw log_activity() "kind" string (whatever the backend happened
// to phrase it as - "DRAFT (awaiting your approval)", "SENT first email",
// "MEETING BOOKED (lead's proposed time)", "DEAD (liveness check failed)",
// "SENT closing reply (not interested - marked dead)", etc.) into a
// friendly icon + label for the end-of-scan popup. Order matters here -
// e.g. the "not interested" close is itself phrased as a "SENT ..." kind,
// so that check has to come before the generic SENT fallback.
function summarizeKind(kind) {
  const k = (kind || "").toUpperCase();
  if (k.startsWith("LEARNED")) return {icon: "📚", label: "New writing rule learned"};
  if (k.includes("NOT INTERESTED")) return {icon: "🚫", label: "Declined — closed out"};
  if (k.includes("MEETING BOOKED")) return {icon: "📅", label: "Meeting booked"};
  if (k.startsWith("DEAD")) return {icon: "💀", label: "Not reachable — marked dead"};
  if (k.startsWith("ERROR")) return {icon: "⚠️", label: "Error handling a reply"};
  if (k.startsWith("DRAFT REJECTED")) return {icon: "🗑️", label: "Draft rejected"};
  if (k.startsWith("DRAFT")) return {icon: "📝", label: "Email drafted — awaiting your approval"};
  if (k.startsWith("REPLY RECEIVED")) return {icon: "📩", label: "Reply received"};
  if (k.startsWith("SENT CALENDLY LINK")) return {icon: "🔗", label: "Sent Calendly booking link"};
  if (k.startsWith("SENT")) return {icon: "📤", label: "Email sent"};
  return {icon: "•", label: kind || "Something happened"};
}

let runSummaryAutoCloseTimer = null;

function closeRunSummary() {
  if (runSummaryAutoCloseTimer) { clearTimeout(runSummaryAutoCloseTimer); runSummaryAutoCloseTimer = null; }
  document.getElementById("run-summary-overlay").classList.remove("show");
}

// Restarts the auto-close countdown - called on show, and again whenever
// the mouse leaves the card (see below), so someone actively reading it
// doesn't have it vanish out from under them.
function armRunSummaryAutoClose() {
  if (runSummaryAutoCloseTimer) clearTimeout(runSummaryAutoCloseTimer);
  runSummaryAutoCloseTimer = setTimeout(closeRunSummary, 7000);
}

// Pops up a little "here's what just happened" card at the end of a scan -
// one line per lead that got touched (drafted, sent, booked, marked dead,
// ...), with a one-line count summary at the top. This is the actual
// answer to "what did the bot just do", rather than trying to narrate it
// live through a progress bar while it's running. It disappears on its
// own after a few seconds - the "Got it" button and click-outside are just
// there for closing it early, not required to dismiss it.
function showRunSummary(events) {
  // "No persona matched" leads get their own file (see the "Persona
  // picks" sidebar link) instead of interrupting this popup - filter them
  // out before anything below even sees them.
  events = events.filter(e => !(e.kind || "").toUpperCase().startsWith("ALERT"));
  const counts = {};
  const order = [];
  events.forEach(e => {
    const s = summarizeKind(e.kind);
    if (!(s.label in counts)) order.push(s.label);
    counts[s.label] = (counts[s.label] || 0) + 1;
  });
  document.getElementById("run-summary-counts").innerText = order.length
    ? order.map(label => `${counts[label]} × ${label}`).join("  ·  ")
    : "Nothing needed doing this round.";

  const listEl = document.getElementById("run-summary-list");
  if (events.length === 0) {
    listEl.innerHTML = '<div class="modal-empty">No leads needed anything this round.</div>';
  } else {
    listEl.innerHTML = events.map(e => {
      const s = summarizeKind(e.kind);
      const who = e.company
        ? `${escapeHtml(e.company)} &lt;${escapeHtml(e.email)}&gt;`
        : escapeHtml(e.email);
      return `<div class="modal-item"><div class="mi-icon">${s.icon}</div>` +
        `<div class="mi-text"><div class="mi-kind">${escapeHtml(s.label)}</div>` +
        `<div class="mi-who" dir="auto">${who}</div></div></div>`;
    }).join("");
  }
  document.getElementById("run-summary-overlay").classList.add("show");
  armRunSummaryAutoClose();
}

let secondsLeft = 0;
let lastRunningNow = false;
let finishedTimer = null;
// null until the first successful poll, so the page never pops up a
// summary for a scan that finished before this tab was even opened -
// only for one that finishes WHILE the tab is open and watching.
let lastSeenRunId = null;

// The bar is a permanent fixture - it never gets hidden. Idle: sits empty.
// Running: fills left to right. Just finished: holds at 100% saying
// "Finished" for a beat, then drains smoothly back down to empty and
// settles on "Idle" - it stays exactly where it always is, it just goes
// back to resting state instead of disappearing.
function setIdleBar() {
  const fill = document.getElementById("progress-fill");
  const wrap = document.getElementById("progress-wrap");
  wrap.dataset.animate = "0";
  wrap.classList.remove("finished");
  fill.classList.remove("indeterminate");
  fill.classList.add("emptying");
  fill.style.width = "0%";
  document.getElementById("progress-text-base").innerText = "Idle";
  document.getElementById("progress-dots").innerText = "";
  document.getElementById("progress-pct").innerText = "";
  document.getElementById("step-dots").innerHTML = "";
}

// Called the moment a run that WAS running just stopped, regardless of what
// the backend's own progress state happens to say by then (a fast run -
// nothing new to contact, no replies waiting - can finish between two
// polls, so this is driven by our own before/after comparison rather than
// trusting a transient backend stage to be caught).
function showFinished() {
  const wrap = document.getElementById("progress-wrap");
  const fill = document.getElementById("progress-fill");
  wrap.dataset.animate = "0";
  wrap.classList.add("finished");
  fill.classList.remove("indeterminate", "emptying");
  fill.style.width = "100%";
  document.getElementById("progress-text-base").innerText = "Finished";
  document.getElementById("progress-dots").innerText = "";
  document.getElementById("progress-pct").innerText = "100%";
  document.getElementById("step-dots").innerHTML = "";
  if (finishedTimer) clearTimeout(finishedTimer);
  finishedTimer = setTimeout(() => {
    setIdleBar();
    finishedTimer = null;
  }, 1400);
}

// A small ticking "..." after the progress text while something's actively
// in flight (a liveness check, a website fetch, an LLM call) - so the bar
// doesn't look frozen between polls just because the number hasn't moved.
let dotFrame = 0;
setInterval(() => {
  const wrap = document.getElementById("progress-wrap");
  if (wrap.dataset.animate !== "1") return;
  dotFrame = (dotFrame + 1) % 4;
  document.getElementById("progress-dots").innerText = ".".repeat(dotFrame);
}, 400);

async function refresh() {
  let data;
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error("server returned " + res.status);
    data = await res.json();
  } catch (err) {
    // Don't just freeze on "loading..." forever - say so, so a backend
    // error is obvious instead of looking like the page is stuck.
    document.getElementById("sub").innerText =
      "Couldn't reach the bot (" + err.message + ") - check the terminal running ui_app.py. Retrying...";
    return;
  }
  const wasRunning = lastRunningNow;
  secondsLeft = data.seconds_left;
  lastRunningNow = data.running_now;

  // A new last_run_id means a scan finished since our last poll - pop up
  // what it did. Skipped on the very first poll (lastSeenRunId still null)
  // so opening the page doesn't immediately show you a summary for
  // whatever scan happened to run before you got here.
  if (lastSeenRunId !== null && data.last_run_id !== lastSeenRunId) {
    showRunSummary(data.last_run_events || []);
  }
  lastSeenRunId = data.last_run_id;

  document.getElementById("batchsize").innerText = data.batch_size;
  document.getElementById("sub").innerText =
    (data.running_now ? "scanning right now... " : "") +
    "last: " + data.last_run_summary +
    (data.review_mode ? " · review mode ON (first emails need approval)" : " · review mode OFF (sends automatically)");
  document.getElementById("sidebar-live-text").innerText =
    data.running_now ? "scanning now" : "next in " + fmtTime(data.seconds_left);

  const runBtn = document.getElementById("run-btn");
  runBtn.disabled = data.running_now;
  runBtn.innerText = data.running_now ? "Running…" : "Run now";

  // KPI stat tiles + the sidebar's own small badges - same counts driving
  // the donut below, just the plain numbers straight from /api/status.
  document.getElementById("stat-queued").innerText = data.queued_count ?? 0;
  document.getElementById("stat-drafts").innerText = data.drafts_count ?? data.drafts.length;
  document.getElementById("stat-attention").innerText = data.attention_count ?? (data.attention || []).length;
  document.getElementById("stat-booked").innerText = data.booked_count ?? 0;
  setNavBadge("nav-draft-badge", data.drafts_count ?? data.drafts.length);
  setNavBadge("nav-attention-badge", data.attention_count ?? (data.attention || []).length);

  // The bar itself is always on screen (see setIdleBar/showFinished) - this
  // just decides what state it's in right now.
  if (wasRunning && !data.running_now) {
    showFinished();
  } else {
    const prog = computeProgress(data.progress);
    if (prog) {
      if (finishedTimer) { clearTimeout(finishedTimer); finishedTimer = null; }
      const wrap = document.getElementById("progress-wrap");
      wrap.dataset.animate = prog.animate ? "1" : "0";
      const fill = document.getElementById("progress-fill");
      fill.classList.remove("emptying");
      fill.classList.toggle("indeterminate", !!prog.indeterminate);
      fill.style.width = prog.indeterminate ? "" : prog.pct + "%";
      document.getElementById("progress-text-base").innerText = prog.text;
      if (!prog.animate) document.getElementById("progress-dots").innerText = "";
      document.getElementById("progress-pct").innerText = prog.indeterminate ? "" : Math.round(prog.pct) + "%";
      renderStepDots(prog.stage, prog.step);
    } else if (!finishedTimer) {
      setIdleBar();
    }
  }

  setBadge("draft-count", data.drafts.length);
  renderDraftList(data.drafts_company, data.drafts_person);


  // Only takes up space on the dashboard when there's actually something
  // to look at - the real-time heads-up is the email the bot already sent
  // you the moment this happened (see send_alert_email in outreach_bot.py),
  // this panel is just where it stays visible until you act on it.
  const attention = data.attention || [];
  setBadge("attention-count", attention.length);
  document.getElementById("attention-panel").style.display = attention.length ? "" : "none";
  fillTable("attention-table", attention, r => `<td dir="auto">${escapeHtml(r.company)}</td><td dir="auto">${escapeHtml(r.job_title || "")}</td><td class="row-email">${escapeHtml(r.email)}</td><td>${statusPill(r.status)}</td><td dir="auto">${escapeHtml(r.notes)}</td><td><button class="reject table-btn js-dismiss-attention" data-email="${escapeAttr(r.email)}">Dismiss</button></td>`);
  document.querySelectorAll("#attention-table .js-dismiss-attention").forEach(btn => {
    btn.addEventListener("click", () => dismissAttention(btn.dataset.email));
  });
}

function fillTable(id, rows, rowFn) {
  const table = document.getElementById(id);
  const cols = table.querySelectorAll("th").length;
  const tbody = table.querySelector("tbody");
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols}" class="empty">Nothing here.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => `<tr>${rowFn(r)}</tr>`).join("");
}

// Drafts used to render as small cards with a clipped body ("show full
// email" toggle) - explicitly asked to go instead of that, since a
// clipped/faded body is exactly what made them hard to read. Now the left
// side is just a compact, clickable ROW per draft (name + one-line subject
// preview, nothing else) and the actual email - full subject, full body,
// never clipped - shows in the big reading pane on the right. Clicking a
// row (or approving/rejecting) just changes which draft is "selected";
// the reading pane always renders the currently selected one in full.
let selectedDraftEmail = null;
let lastDraftsCompany = [];
let lastDraftsPerson = [];
// Every poll (every 1.2-3s, see schedulePoll below) used to unconditionally
// rebuild these two panes' innerHTML from scratch - even when nothing in
// them had actually changed. Replacing innerHTML resets scroll position to
// the top, so if you were mid-scroll reading a long email, or scrolled
// partway down the draft list, the next poll yanked you straight back up -
// over and over, every few seconds. Fixed by only touching the DOM when
// the content genuinely changed (tracked via these signature strings);
// an unchanged poll now does nothing at all to either pane.
let lastDraftListSig = null;
let lastPaneSig = null;

function draftRowHtml(d) {
  const nameLine = d.kind === "person" && d.contact_name ? d.contact_name : d.company;
  const subLine = d.kind === "person" && d.contact_name ? d.company : d.email;
  const active = d.email === selectedDraftEmail ? " active" : "";
  return `
    <div class="draft-row kind-${d.kind}${active}" data-email="${escapeAttr(d.email)}">
      <div class="row-name" dir="auto">${escapeHtml(nameLine || d.email)}</div>
      <div class="row-sub" dir="auto">${escapeHtml(subLine || "")}</div>
      <div class="row-subject" dir="auto">${escapeHtml(d.subject || "")}</div>
    </div>
  `;
}

function renderDraftList(companies, people) {
  lastDraftsCompany = companies || [];
  lastDraftsPerson = people || [];
  const all = [...lastDraftsCompany, ...lastDraftsPerson];

  // If the previously-selected draft is gone (approved/rejected elsewhere,
  // or this is the very first load), fall back to the first draft in the
  // list so the reading pane is never left showing a stale/vanished email.
  if (selectedDraftEmail === null || !all.some(d => d.email === selectedDraftEmail)) {
    selectedDraftEmail = all.length ? all[0].email : null;
  }

  // Signature covers everything the rendered list actually depends on -
  // which drafts exist, their content, and which row is highlighted as
  // active. If none of that changed since the last poll, skip touching
  // the DOM entirely so a mid-scroll read of the list is never disturbed.
  const sig = JSON.stringify({
    sel: selectedDraftEmail,
    c: lastDraftsCompany.map(d => [d.email, d.subject, d.body]),
    p: lastDraftsPerson.map(d => [d.email, d.contact_name, d.company, d.subject, d.body]),
  });
  if (sig === lastDraftListSig) {
    renderReadingPane();
    return;
  }
  lastDraftListSig = sig;

  const el = document.getElementById("draft-list");
  const savedScroll = el.scrollTop;

  if (all.length === 0) {
    el.innerHTML = '<div class="empty">Nothing waiting on you right now.</div>';
  } else {
    let html = "";
    if (lastDraftsCompany.length) {
      html += `<div class="list-group-label">Companies <span class="badge">${lastDraftsCompany.length}</span></div>`;
      html += lastDraftsCompany.map(draftRowHtml).join("");
    }
    if (lastDraftsPerson.length) {
      html += `<div class="list-group-label">People <span class="badge">${lastDraftsPerson.length}</span></div>`;
      html += lastDraftsPerson.map(draftRowHtml).join("");
    }
    el.innerHTML = html;
  }

  // Real event listeners, not inline onclick="..." - see escapeAttr's
  // comment above for why inline onclick with a dynamic email value was
  // silently breaking every row's markup.
  el.querySelectorAll(".draft-row").forEach(row => {
    row.addEventListener("click", () => selectDraft(row.dataset.email));
  });

  // The list only actually needed rebuilding here because something in it
  // changed (a new draft, one disappearing, a selection change) - not
  // because you scrolled it. Put the scroll position back so a genuine
  // content change elsewhere in the list doesn't also yank you back to
  // the top of it.
  el.scrollTop = savedScroll;

  renderReadingPane();
}

function selectDraft(email) {
  selectedDraftEmail = email;
  renderDraftList(lastDraftsCompany, lastDraftsPerson);
}

function renderReadingPane() {
  const pane = document.getElementById("reading-pane");
  const all = [...lastDraftsCompany, ...lastDraftsPerson];
  const d = all.find(x => x.email === selectedDraftEmail);

  // Same fix as the list: skip rebuilding entirely when the pane would
  // render identically to last time (same draft, same subject/body) -
  // this is what stops every 1.2-3s poll from resetting your scroll
  // position back to the top of the email you're in the middle of
  // reading. A genuine change (switching drafts, or this draft's content
  // changing) still rebuilds normally, and starts that new content at the
  // top - which is correct there, not a bug.
  const sig = d ? JSON.stringify([d.email, d.subject, d.body]) : null;
  if (sig === lastPaneSig) return;
  lastPaneSig = sig;

  if (!d) {
    pane.innerHTML = '<div class="reading-empty">Select a draft on the left to read it in full.</div>';
    return;
  }
  const nameLine = d.kind === "person" && d.contact_name ? d.contact_name : d.company;
  const subLine = d.kind === "person" && d.contact_name ? d.company : "";
  const displayName = nameLine || d.email;
  // Outlook-style message chrome: a slim toolbar (Approve/Reject standing
  // in for Send/Discard), the subject large, then a From/To row with a
  // colored initials avatar - same layout an actual reading pane uses.
  // dir="auto" on the subject/body/from-name lets the browser pick
  // left-to-right or right-to-left PER STRING based on its own first
  // strong-direction character - correct whether a given draft's content
  // is Hebrew, English, or a mix, without any manual language detection.
  pane.innerHTML = `
    <div class="ol-toolbar">
      <button class="js-approve">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        Approve &amp; send
      </button>
      <button class="reject js-reject">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        Reject
      </button>
      <span class="status-pill kind-${d.kind} ol-kind-pill" style="margin-left:auto;">${d.kind === "person" ? "Person" : "Company"}</span>
    </div>
    <div class="ol-subject" dir="auto">${escapeHtml(d.subject || "(no subject)")}</div>
    <div class="ol-meta-row">
      <div class="ol-avatar" style="background:${avatarColorFor(displayName)}">${escapeHtml(initialsFor(displayName))}</div>
      <div class="ol-meta-text">
        <div class="ol-from" dir="auto">${escapeHtml(displayName)}${subLine ? " &middot; " + escapeHtml(subLine) : ""} <span class="ol-email">&lt;${escapeHtml(d.email)}&gt;</span></div>
        <div class="ol-to">To: you (draft - not sent yet)</div>
      </div>
    </div>
    <div class="reading-body-wrap"><div class="reading-body" dir="auto">${escapeHtml(d.body || "")}</div></div>
  `;
  // Closing over d.email directly here (rather than a data-* attribute)
  // since these two buttons are only ever built for the one currently-
  // selected draft - no list/re-lookup needed.
  pane.querySelector(".js-approve").addEventListener("click", () => approve(d.email));
  pane.querySelector(".js-reject").addEventListener("click", () => reject(d.email));
}

// "In line" now shows exactly the next row each table would hand
// _run_once() on the next run - one company + one person, same as the bot
// itself picks (see _eligible_new()/batch in outreach_bot.py) - plus how
// many more are behind it in that table's queue.
0async function approve(email) {
  await fetch("/api/approve", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({email})});
  refresh();
}
async function reject(email) {
  await fetch("/api/reject", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({email})});
  refresh();
}
async function dismissAttention(email) {
  // Clears the flag (sets status back to "replied") so this row drops off
  // the "Needs your attention" panel - use it once you've handled the
  // reply yourself (in Gmail directly, or you just decided nothing more is
  // needed). The bot keeps watching for a future reply from this lead
  // afterward, same as any other ongoing conversation.
  await fetch("/api/dismiss-attention", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({email})});
  refresh();
}
function downloadRecap() {
  // A plain navigation, not fetch+blob - the server sends the PDF with
  // Content-Disposition: attachment, so the browser just downloads it (or
  // opens it in a PDF tab) with no extra JS needed, and it works the same
  // for a slow-to-generate recap as a fast one.
  window.location.href = "/api/daily-recap";
}
async function runNow() {
  // Show something immediately rather than waiting for the next poll to
  // catch it - a run with nothing new to do can finish in well under a
  // second, faster than the poll interval would ever notice on its own.
  lastRunningNow = true;
  if (finishedTimer) { clearTimeout(finishedTimer); finishedTimer = null; }
  const wrap = document.getElementById("progress-wrap");
  wrap.dataset.animate = "1";
  wrap.classList.remove("finished");
  const fill = document.getElementById("progress-fill");
  fill.classList.remove("indeterminate", "emptying");
  fill.style.width = "4%";
  document.getElementById("progress-text-base").innerText = "Starting scan";
  document.getElementById("progress-dots").innerText = "";
  document.getElementById("progress-pct").innerText = "";
  document.getElementById("step-dots").innerHTML = "";
  const runBtn = document.getElementById("run-btn");
  runBtn.disabled = true;
  runBtn.innerText = "Running…";

  // Critical: re-arm the poll loop on the FAST cadence right now. Without
  // this, the poll timer already in flight was scheduled the last time
  // schedulePoll() ran - which, before this run started, saw
  // lastRunningNow=false and armed itself for the SLOW 15s cadence. That
  // timer doesn't magically speed up just because lastRunningNow flipped
  // to true up above; it fires whenever it was originally going to fire.
  // A scan that finishes in a couple of seconds (nothing new, no replies)
  // can then complete entirely inside that dead 15s gap - so the page
  // sits frozen on this optimistic "Starting scan" frame the whole time,
  // never shows a single real step, and then jumps straight to
  // "Finished" on the one poll that does land. Re-arming here is what
  // actually makes the detailed progress visible instead of just hoping
  // the timing works out.
  schedulePoll();

  await fetch("/api/run-now", {method: "POST"});
  await refresh();
}

setInterval(() => {
  secondsLeft = Math.max(0, secondsLeft - 1);
  document.getElementById("timer").innerText = fmtTime(secondsLeft);
}, 1000);

// Poll quickly while a scan is actually running (so the progress bar feels
// live) and fall back to a relaxed interval the rest of the time - no
// point hammering the server every second while nothing's happening. The
// idle interval is kept short (3s, not something lazier like 15s) because
// an automatic scheduled scan (the 20-minute timer, not the "Run now"
// button) can start and finish faster than a long idle interval would
// ever notice - the button click has its own fix (see runNow()) but a
// scheduled run has no click to hook, so the only guard against missing
// it entirely is not leaving a long gap between polls in the first place.
let pollTimer = null;
function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await refresh();
    schedulePoll();
  }, lastRunningNow ? 1200 : 3000);
}

refresh().then(schedulePoll);

// The activity chart only changes with real sent/booked emails, which is
// far slower than the status poll above - loaded once up front and then
// re-checked every few minutes (cheap: it's a no-op re-render unless the
// data actually changed, see loadActivitySeries's signature check).
loadActivitySeries();
setInterval(loadActivitySeries, 5 * 60 * 1000);
</script>
</body>
</html>
"""


_scheduler_thread_started = False

# Starts the scheduler thread the moment this module is imported, not just
# when run directly - this is what makes the 20-minute scan loop actually
# fire under gunicorn (`gunicorn ui_app:app`, the way Railway/Render run
# it): gunicorn imports this module as "ui_app", it never executes as
# "__main__", so a thread started only inside `if __name__ == "__main__"`
# would silently never run and the dashboard would sit there forever
# showing "Idle" with nothing scanning. Guarded so re-importing this module
# (e.g. from a test) doesn't spawn a second scheduler thread.
if not _scheduler_thread_started:
    threading.Thread(target=scheduler_loop, daemon=True).start()
    _scheduler_thread_started = True
    print(f"Outreach bot scheduler [{BUILD_TAG}] started - scans every "
          f"{RUN_INTERVAL_SECONDS // 60} min, {bot.BATCH_SIZE} leads per scan.")

if __name__ == "__main__":
    # Local dev only - production runs this through gunicorn instead (see
    # Procfile), which is what actually binds $PORT on Railway/Render.
    port = int(os.getenv("PORT", "5050"))
    print(f"Outreach bot dashboard [{BUILD_TAG}]: http://127.0.0.1:{port}")
    print(f"  -> if the page you open doesn't show '{BUILD_TAG}' in its "
          f"bottom-right corner, you're looking at a stale browser tab or an "
          f"old process still holding the port - not this file. Hard-refresh "
          f"(Ctrl+F5) and/or make sure no other 'python ui_app.py' is still "
          f"running before assuming a fix didn't work.")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)