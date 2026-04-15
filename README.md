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


i already created a bot called isw_trading_bot that you can take inspiration from
for exemple there is already a script called manage_city_market_map that collectes that active territorial markets in ukraine and maps them to ISW city layer features and display them on the map

there is also all that is needed in the main bot to scrape ISW layers, although this bot version is optimized for speed and not completeness since execution time is crucial for bots, we dont have this constraint for wardotfun so we can make a much simpler design
