# Protect "highly used items" at Wikidata with an admin bot
This is a Wikidata admin bot that manages page protections of “highly used items” as defined in the [page protection policy](https://www.wikidata.org/wiki/Wikidata:Protection_policy) of Wikidata.

Currently the bot runs weekly on [Toolforge](https://wikitech.wikimedia.org/wiki/Portal:Toolforge), using Python 3.11.2 and the [shared pywikibot files](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Pywikibot#Using_the_shared_Pywikibot_files_(recommended_setup)) in a Kubernetes environment via [User:MsynABot](https://www.wikidata.org/wiki/User:MsynABot). The Wikidata account needs admin rights to successfully perform this task.

In the background, the bot relies on [wdcm_topItems.csv](https://analytics.wikimedia.org/published/datasets/wmde-analytics-engineering/wdcm/etl/wdcm_topItems.csv) from the [Wikidata Concepts Monitor](https://wikidata-analytics.wmcloud.org/app/WikidataAnalytics).