# war.fun
wardotfun is a website where people go to get info into ongoing wars and bet on it through prediction markets. 

We will add multiple theatres eventually but we will start with Ukraine and especially territorial changes/strikes markets

the basic interface will be centered around a map of ukraine with cities that host markets (capture or strikes) highlighted and clickable, you should have info on the market clicking on the city and eventually be able to bet on it.

The map is also overlayed with the map used for resolution (institute for the study of war) but eventually we also want to add other mapper the the user can switch through. we also want the user to be able to backtrack in time ideally

relevant geolocations need to be indicated on the map, they will be collected from X, telegram as well as other websites.

we also need a feed for map changes, these are ususally published through the telegram canals of most mappers.

we will proceed by phases of complexity.

phase 1 :

- basic interface with the ukraine map 
- ISW map layers overlay
- cities with markets highlighted and clickable with info about the markets and polymarket links to bet on it

phase 2 :

- multiple mappers overlay

i already created a bot called isw_trading_bot that you can take inspiration from
for exemple there is already a script called manage_city_market_map that collects territorial markets in Ukraine and maps them to ISW city layer features.

## Market archive

`utils/manage_city_market_map.py` maintains `backend/data/city_market_map.json` as a persistent archive. Markets are never deleted: active markets are displayed on the map, confirmed settlements are marked `resolved`, and ambiguous closures are retained as `closed`. The updater also backfills closed markets from known Polymarket event families.

Run the updater with:

```bash
source env/bin/activate
python utils/manage_city_market_map.py
```

After updating while the server is running, reload the archive with `POST /api/admin/reload-city-map`.

The backend also runs this job non-interactively at startup and every 24 hours. Markets requiring city selection or target coordinates are skipped for later manual processing. Scheduler state is available at `GET /api/admin/market-map-updater`.

The schedule can be configured with:

- `MARKET_MAP_UPDATER_ENABLED=0` disables the background job.
- `MARKET_MAP_UPDATE_INTERVAL` changes the successful-run interval in seconds.
- `MARKET_MAP_UPDATE_RETRY_INTERVAL` changes the retry interval after failures.
- `MARKET_MAP_UPDATE_INITIAL_DELAY` changes the first-run delay when no successful run is recorded.

there is also all that is needed in the main bot to scrape ISW layers, although this bot version is optimized for speed and not completeness since execution time is crucial for bots, we dont have this constraint for wardotfun so we can make a much simpler design
