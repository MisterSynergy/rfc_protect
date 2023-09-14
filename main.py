"""
Author:   https://www.wikidata.org/wiki/User:MisterSynergy
License:  MIT license
Version:  2023-09-14
Task:     manage item page protections of "highly used items" in Wikidata
See also: https://www.wikidata.org/wiki/User:MsynABot
"""


import sys
from collections import namedtuple
from io import TextIOWrapper
from json import loads
from os.path import expanduser
from time import strftime, gmtime, sleep
from typing import Any, TypedDict, Union

import mariadb
import pandas as pd
import pywikibot as pwb
from requests import get


#### Simple configuration class
class Config:
    ## Script run time
    SCRIPTRUNTIME = gmtime()

    ## Database access (user credentials come from elsewhere) ##
    REPLICA_HOST:str = 'wikidatawiki.analytics.db.svc.wikimedia.cloud'
    REPLICA_DATABASE:str = 'wikidatawiki_p'

    ## edit summaries for protection level changes
    PROTECT_REASON:str = 'Highly used item: to be indefinitely semi-protected per [[:d:Wikidata:Protection policy#Highly used items|Wikidata:Protection policy#Highly used items]]; please use [[Template:Edit request]] on the item talk page if you cannot edit this item'
    UNPROTECT_REASON:str = 'Item is no longer highly used as per [[:d:Wikidata:Protection policy#Highly used items|Wikidata:Protection policy#Highly used items]]'

    ## External resources ##
    WDCM_TOPLIST_URL:str = 'https://analytics.wikimedia.org/published/datasets/wmde-analytics-engineering/wdcm/etl/wdcm_topItems.csv'
    BLACKLIST_URL:str = 'https://www.wikidata.org/w/index.php?title=User:MsynABot/rfc-protect-blacklist.json&action=raw&ctype=application/json'
    WD_API_ENDPOINT:str = 'https://www.wikidata.org/w/api.php'

    ## Files, log files ##
    FILE_EARLYPROTECTIONS:str = './dataframe/earlyItemProtections.txt'
    FILE_TOPLIST:str = './dataframe/wdcmToplist.txt'
    LOG_INDEFSEMI:str = './dataframe/indefSemiprotectedItems.tsv'
    LOG_PROTECTIONSTOADD:str = './dataframe/protectionToAdd.tsv'
    LOG_PROTECTIONSTOLIFT:str = './dataframe/protectionToLift.tsv'
    LOG_PROTECTIONSINCOOLDOWN:str = './dataframe/protectionInCooldown.tsv'
    LOG_PROTECTIONSNOTOTLIFT:str = './dataframe/protectionNotToLift.tsv'
    LOG_PROTECTEDHIGHLYUSED:str = './dataframe/protectedHighlyUsed.tsv'
    LOG_PROTECTEDNOTHIGHLYUSED:str = './dataframe/protectedNotHighlyUsed.tsv'
    LOG_PROTECTIONALREADYSET:str = './dataframe/protectedAlreadySet.tsv'

    REPORT_LOG_PATTERN:str = './log/rfc-protect_{timestmp}.log'
    REPORT_TEMPLATE:str = './report/template.txt'
    REPORT_FILE:str = './report/report.txt'
    REPORT_PAGE:str = 'User:MsynABot/rfc-protect-report'
    REPORT_EDITSUMMARY:str = 'update page protection management statistics #msynabot'

    WRITETOTERMINAL:bool = False
    WRITETOLOG:bool = True

    ## task configuration ##
    WHITELISTED_ADMINS:list[str] = [ 'MsynABot' ]  # protections added by these accounts may be removed
    ENTITYUSAGELIMIT:int = 500 # per RfC
    COOLDOWNUSAGELIMIT:int = 300 # not in RfC, but as a measure to prevent frequent protection changes of items near ENTITYUSAGELIMIT
    ADDLIMIT:Union[int, None] = 1000 # int or None; do not add any protections when more than this number needs to be added
    LIFTLIMIT:Union[int, None] = 100 # int or None; do not lift any protections when more than this number needs to be lifted
    MINSUBSCRIBEDPROJECTS:Union[int, None] = None # int or None; minimum number of subscribed projects; usually includes wikidatawiki
    SIMULATE:bool = False # do not make any protection changes when True; any other value is equivalent to False
    HARDLIMIT:Union[int, None] = None # int or None; max number of cases processed for protection removals and additions
    SLEEPAFTEREDIT:int = 5 # seconds

    ## set up pywikibot ##
    WIKIDATA_SITE:pwb.Site = pwb.Site('wikidata', 'wikidata')
    WIKIDATA_SITE.login()
    WIKIDATA_REPO:pwb.site._datasite.DataSite = WIKIDATA_SITE.data_repository()

    ## technical variables ##
    UNPROTECTED:dict[str, tuple[str, str]] = {}
    SEMIPROTECTED:dict[str, tuple[str, str]] = {'edit': ('autoconfirmed', 'infinity')}


#### for typing hints
Case = namedtuple('Case', ['qid', 'entityUsageCount', 'username'])
CounterDict = TypedDict('CounterDict', {'msg': str, 'cnt': int})
InputVars = TypedDict('InputVars', {
    'timestmp' : str,
    'wdcmurl' : str,
    'blacklisturl' : str,
    'entityusagelimit' : int,
    'cooldownlimit' : int,
    'addlimit' : Union[int, None],
    'liftlimit' : Union[int, None],
    'hardlimit' : Union[int, None],
    'minsubscribedprojects' : Union[int, None],
    'wdcmcnt' : int,
    'wdcmusagelimit' : int,
    'wdcmpercent' : float,
    'itemcnt' : int,
    'blacklistedcnt' : int,
    'indefsemi' : int,
    'indefsemihighlyused' : int,
    'indefsemiother' : int,
    'indefsemiotherbutalsohighlyused' : int,
    'protectionstoadd' : int,
    'protectionstolift' : int,
    'cooldowncnt' : int,
    'cooldownlist' : str,
    'addedcnt' : int,
    'liftedcnt' : int,
    'additionstats' : str,
    'removalstats' : str
})


#### Enable logging into file
class Logger(TextIOWrapper):
    def __init__(self, write_to_terminal:bool=False, write_to_log:bool=True) -> None:
        self.write_to_terminal = write_to_terminal
        self.write_to_log = write_to_log

        self.terminal = sys.stdout
        if self.write_to_log is True:
            self.log = open(Config.REPORT_LOG_PATTERN.format(timestmp=strftime('%Y%m%d_%H%M%S', Config.SCRIPTRUNTIME)), 'a')

    def write(self, message:str) -> int:
        if self.write_to_terminal is True:
            self.terminal.write(message)
        if self.write_to_log is True:
            self.log.write(message)
        return len(message)

    def flush(self):
        #this flush method is needed for python 3 compatibility.
        pass


#### Counting cases
class Counter:
    added_protection:dict[str, CounterDict] = {
        'belowlimit' : { 'msg' : f'Entity usage below configured limit of {Config.ENTITYUSAGELIMIT}', 'cnt' : 0 },
        'blacklisted' : { 'msg' : 'Blacklisted item', 'cnt' : 0 },
        'belowsubscribedprojects' : { 'msg' : f'Subscribed project count below configured limit of {Config.MINSUBSCRIBEDPROJECTS}', 'cnt' : 0 },
        'itemnotexists' : { 'msg' : 'Item page does not exist', 'cnt' : 0 },
        'itemisredirect' : { 'msg' : 'Item page is a redirect', 'cnt' : 0 },
        'itemhassomeprotection' : { 'msg' : 'Item page already has some sort of protection', 'cnt' : 0 },
        'couldntchangetosemiprotection' : { 'msg' : 'Addition of protection failed', 'cnt' : 0 },
        'savefailed' : { 'msg' : 'Modification of protection level failed', 'cnt' : 0 },
        'successful' : { 'msg' : 'Successfully protected', 'cnt' : 0 }
    }
    removed_protection:dict[str, CounterDict] = {
        'overlimit' : { 'msg' : f'Entity usage above configured limit of {Config.ENTITYUSAGELIMIT}', 'cnt' : 0 },
        'notwhitelisted' : { 'msg' : 'Not a whitelisted protection', 'cnt' : 0 },
        'itemnotexists' : { 'msg' : 'Item page does not exist', 'cnt' : 0 },
        'itemisredirect' : { 'msg' : 'Item page is a redirect', 'cnt' : 0 },
        'itemisnotsemiprotected' : { 'msg' : 'Item page is not indefinitely semiprotected', 'cnt' : 0 },
        'couldntremoveprotection' : { 'msg' : 'Removal of protection failed', 'cnt' : 0 },
        'savefailed' : { 'msg' : 'Modification of protection level failed', 'cnt' : 0 },
        'successful' : { 'msg' : 'Successfully unprotected', 'cnt' : 0 }
    }

    @staticmethod
    def add_protection(key:str) -> None:
        if key in Counter.added_protection:
            Counter.added_protection[key]['cnt'] += 1

    @staticmethod
    def remove_protection(key:str) -> None:
        if key in Counter.removed_protection:
            Counter.removed_protection[key]['cnt'] += 1

    @staticmethod
    def make_table(dct:dict[str, CounterDict]) -> str:
        base = """{{| class="wikitable sortable"
|-
! processing result !! number of cases
{table_body}|}}"""
        tablerow = """|-
| {msg} || {cnt}
"""
        table_body = ''
        for value in dct.values():
            if value['cnt'] > 0:
                table_body += tablerow.format(msg=value['msg'], cnt=value['cnt'])

        return base.format(table_body=table_body)


#### Manage database access to the Wikidata replica database on Toolforge
class ReplicaCursor:
    def __init__(self):
        self.replica = mariadb.connect(
            host=Config.REPLICA_HOST,
            database=Config.REPLICA_DATABASE,
            default_file=f'{expanduser("~")}/replica.my.cnf'
        )
        self.cursor = self.replica.cursor(dictionary=True)

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cursor.close()
        self.replica.close()


def access_replica_database(query:str) -> list[dict[str, Any]]:
    with ReplicaCursor() as cursor:
        cursor.execute(query)
        result = cursor.fetchall()

    return result


#### Collect data
# From database: indefinitely semiprotected items
def get_indefinitely_semiprotected_items() -> pd.DataFrame:
    sql = """SELECT
      CONVERT(log_title USING utf8) AS qid,
      CONVERT(log_timestamp USING utf8) AS logTimestamp,
      CONVERT(actor_name USING utf8) AS username
    FROM
      page_restrictions
        JOIN logging ON pr_page=log_page
        JOIN actor_logging ON log_actor=actor_id
    WHERE
      pr_type='edit'
      AND pr_level='autoconfirmed'
      AND pr_expiry='infinity'
      AND log_namespace=0
      AND log_type='protect'
      AND log_action IN ('protect', 'modify')"""

    query_result = access_replica_database(sql)

    indef_semiprotected_items = pd.DataFrame(
        data=query_result
    )
    indef_semiprotected_items['logTimestamp'] = indef_semiprotected_items['logTimestamp'].astype(int)

    indef_semiprotected_items = indef_semiprotected_items.sort_values(by='logTimestamp', ascending=True).drop_duplicates(subset='qid', keep='last')
    indef_semiprotected_items.to_csv(Config.LOG_INDEFSEMI, sep='\t')

    return indef_semiprotected_items


# Hardcoded file: early protections that the bot is allowed to lift
def get_early_item_protections() -> pd.DataFrame:
    early_item_protections = pd.read_csv(
        Config.FILE_EARLYPROTECTIONS,
        sep='\t',
        names=[ 'qid', 'logId', 'admin' ],
        dtype={ 'qid' : 'str', 'logId' : 'int', 'admin' : 'str' }
    ).sort_values(by='logId', ascending=True).drop_duplicates(subset='qid', keep='last')

    return early_item_protections


# From WDCM: list of highly used items (500+ uses)
def get_list_of_highly_used_items_from_wdcm() -> pd.DataFrame:
    # pandas does not like https resources; we thus cache the file locally
    with open(Config.FILE_TOPLIST, mode='w', encoding='utf8') as file_handle:
        file_handle.write(get(Config.WDCM_TOPLIST_URL).text)

    wdcm_toplist = pd.read_csv(
        Config.FILE_TOPLIST,
        names=[ 'qid', 'entityUsageCount' ],
        dtype={ 'qid' : 'str', 'entityUsageCount' : 'int' },
        header=0
    )

    return wdcm_toplist


# From Wikidata: list of items that should not be protected (sandbox, etc.)
def get_blacklisted_items() -> list[str]:
    return loads(get(Config.BLACKLIST_URL).text)


def get_number_of_subscribed_projects(qid:str) -> int:
    data = loads(
        get(
            url=Config.WD_API_ENDPOINT,
            params={
                'action' : 'query',
                'list' : 'wbsubscribers',
                'wblsentities' : qid,
                'wblslimit' : '500',
                'format' : 'json'
            }
        ).text
    )

    subscribers = []
    for elem in data['query']['subscribers'][qid]['subscribers']:
        subscribers.append(elem['site'])

    return len(subscribers)


def get_number_of_items() -> int:
    data = loads(
        get(
            url=Config.WD_API_ENDPOINT,
            params={
                'action' : 'query',
                'meta' : 'siteinfo',
                'siprop' : 'statistics',
                'format' : 'json'
            }
        ).text
    )

    return int(data['query']['statistics']['articles'])


# Compute sets of items meeting certain conditions; usually pandas dataframes
def protected_highly_used_items(protected:pd.DataFrame, early:pd.DataFrame) -> pd.DataFrame:
    protected_highly_used_items_df = protected[(protected['qid'].isin(early['qid'])) | (protected['username'].isin(Config.WHITELISTED_ADMINS))]
    protected_highly_used_items_df.to_csv(Config.LOG_PROTECTEDHIGHLYUSED, sep='\t')

    return protected_highly_used_items_df


def protected_not_highly_used_items(protected:pd.DataFrame, early:pd.DataFrame) -> pd.DataFrame:
    protected_not_highly_used_items_df = protected[(~protected['qid'].isin(early['qid'])) & (~protected['username'].isin(Config.WHITELISTED_ADMINS))]
    protected_not_highly_used_items_df.to_csv(Config.LOG_PROTECTEDNOTHIGHLYUSED, sep='\t')

    return protected_not_highly_used_items_df


def protection_missing(top:pd.DataFrame, protected:pd.DataFrame) -> pd.DataFrame:
    item_protection_to_add = top[(~top['qid'].isin(protected['qid'])) & (top['entityUsageCount']>=Config.ENTITYUSAGELIMIT)].dropna().sort_values(by='entityUsageCount', ascending=False)
    item_protection_to_add.to_csv(Config.LOG_PROTECTIONSTOADD, sep='\t')

    return item_protection_to_add


def protection_to_lift(top:pd.DataFrame, protected:pd.DataFrame) -> pd.DataFrame:
    item_protection_to_lift = protected[~protected['qid'].isin(top.loc[top['entityUsageCount']>=Config.COOLDOWNUSAGELIMIT, 'qid'])].dropna().merge(top, on='qid', how='left').sort_values(by='entityUsageCount', ascending=False)
    item_protection_to_lift.to_csv(Config.LOG_PROTECTIONSTOLIFT, sep='\t')

    return item_protection_to_lift


def protection_in_cooldown(top:pd.DataFrame, protected:pd.DataFrame) -> pd.DataFrame:
    item_protection_in_cooldown = protected[(protected['qid'].isin(top.loc[top['entityUsageCount']<Config.ENTITYUSAGELIMIT, 'qid'])) & (protected['qid'].isin(top.loc[top['entityUsageCount']>=Config.COOLDOWNUSAGELIMIT, 'qid']))].dropna().merge(top, on='qid', how='left').sort_values(by='entityUsageCount', ascending=False)
    item_protection_in_cooldown.to_csv(Config.LOG_PROTECTIONSINCOOLDOWN, sep='\t')

    return item_protection_in_cooldown


def protection_already_set(top:pd.DataFrame, protected:pd.DataFrame) -> pd.DataFrame:
    highly_used_items_already_protected_for_other_reasons = protected[protected['qid'].isin(top.loc[top['entityUsageCount']>=Config.ENTITYUSAGELIMIT, 'qid'])]
    highly_used_items_already_protected_for_other_reasons.to_csv(Config.LOG_PROTECTIONALREADYSET, sep='\t')

    return highly_used_items_already_protected_for_other_reasons


def is_whitelisted_early_protection(case:Case, early_protection:pd.DataFrame) -> bool:
    if early_protection.shape[0] != 1: # not whitelisted then
        return False

    sql = f"""SELECT
      CONVERT(log_title USING utf8) AS log_qid,
      log_id,
      CONVERT(actor_name USING utf8) AS log_actorname
    FROM
      logging
        JOIN actor_logging ON log_actor=actor_id
    WHERE
      log_action='protect'
      AND log_type='protect'
      AND log_namespace=0
      AND log_title='{case.qid}'
    ORDER BY
      log_timestamp DESC
    LIMIT
      1"""
    log_info = access_replica_database(sql)

    log_qid = log_info[0].get('log_qid', '')
    log_id = log_info[0].get('log_id', 0)
    log_actorname = log_info[0].get('log_actorname', '')

    return log_qid==early_protection.iloc[0].at['qid'] and log_id==early_protection.iloc[0].at['logId'] and log_actorname==early_protection.iloc[0].at['admin']


# Actually change protection level here
def add_protection(case:Case, blacklist:list[str]) -> str:
    if case.entityUsageCount < Config.ENTITYUSAGELIMIT:
        Counter.add_protection('belowlimit')
        raise RuntimeWarning(f'Item {case.qid}: entity usage count {case.entityUsageCount} under {Config.ENTITYUSAGELIMIT}')
    if case.qid in blacklist:
        Counter.add_protection('blacklisted')
        raise RuntimeWarning(f'Item {case.qid}: item is blacklisted on the onwiki blacklist')
    if Config.MINSUBSCRIBEDPROJECTS is not None and get_number_of_subscribed_projects(case.qid) < Config.MINSUBSCRIBEDPROJECTS:
        Counter.add_protection('belowsubscribedprojects')
        raise RuntimeWarning(f'Item {case.qid}: fewer than {Config.MINSUBSCRIBEDPROJECTS} subscribed projects')
    q_item = pwb.ItemPage(Config.WIKIDATA_REPO, case.qid)
    if not q_item.exists():
        Counter.add_protection('itemnotexists')
        raise RuntimeWarning(f'Item {case.qid} does not exist')
    if q_item.isRedirectPage():
        Counter.add_protection('itemisredirect')
        raise RuntimeWarning(f'Item {case.qid} is a redirect')
    current_protection = q_item.protection()
    if current_protection != Config.UNPROTECTED:
        Counter.add_protection('itemhassomeprotection')
        raise RuntimeWarning(f'Item {case.qid} is currently not unprotected (but {current_protection} instead)')
    current_protection['edit'] = ('autoconfirmed','infinity')
    if current_protection != Config.SEMIPROTECTED:
        Counter.add_protection('couldntchangetosemiprotection')
        raise RuntimeWarning(f'Item {case.qid}: modified protection is not "semiprotected" (but {current_protection} instead)')
    try:
        if Config.SIMULATE is not True:
            q_item.protect(
                reason=Config.PROTECT_REASON,
                protections={'edit':'autoconfirmed'},
                expiry='infinity'
            )
        sleep(Config.SLEEPAFTEREDIT)
    except RuntimeError as exception:
        Counter.add_protection('savefailed')
        raise exception
    else:
        Counter.add_protection('successful')
        return f'Item {case.qid}: added protection to {current_protection} (used on {case.entityUsageCount} pages)'


def remove_protection(case:Case, early_protection:pd.DataFrame) -> str:
    if case.entityUsageCount >= Config.ENTITYUSAGELIMIT:
        Counter.remove_protection('overlimit')
        raise RuntimeWarning(f'Item {case.qid}: entity usage count {case.entityUsageCount} over {Config.ENTITYUSAGELIMIT}')
    if case.username not in Config.WHITELISTED_ADMINS and not is_whitelisted_early_protection(case, early_protection):
        Counter.remove_protection('notwhitelisted')
        raise RuntimeWarning(f'Item {case.qid}: user name of protecting admin "{case.username}" not whitelisted and not a whitelisted early protection')
    q_item = pwb.ItemPage(Config.WIKIDATA_REPO, case.qid)
    if not q_item.exists():
        Counter.remove_protection('itemnotexists')
        raise RuntimeWarning(f'Item {case.qid} does not exist')
#   if q_item.isRedirectPage():
#       Counter.remove_protection('itemisredirect')
#       raise RuntimeWarning(f'Item {case.qid} is a redirect')
    current_protection = q_item.protection()
    if current_protection != Config.SEMIPROTECTED:
        Counter.remove_protection('itemisnotsemiprotected')
        raise RuntimeWarning(f'Item {case.qid} is currently not semiprotected (but {current_protection} instead)')
    current_protection.pop('edit')
    if current_protection != Config.UNPROTECTED:
        Counter.remove_protection('couldntremoveprotection')
        raise RuntimeWarning(f'Item {case.qid}: modified protection is not "unprotected" (but {current_protection} instead)')
    try:
        if Config.SIMULATE is not True:
            q_item.protect(
                reason=Config.UNPROTECT_REASON,
                protections={'edit':'all'}
            )
        sleep(Config.SLEEPAFTEREDIT)
    except RuntimeError as exception:
        Counter.remove_protection('savefailed')
        raise exception
    else:
        Counter.remove_protection('successful')
        return f'Item {case.qid}: removed protection to {current_protection} (used on {case.entityUsageCount} pages)'


# Reporting to an onwiki page
def make_report(input_vars:InputVars) -> None:
    with open(Config.REPORT_TEMPLATE, mode='r', encoding='utf8') as file_handle:
        template = file_handle.read()

    report = template.format(**input_vars)

    with open(Config.REPORT_FILE, mode='w', encoding='utf8') as file_handle:
        file_handle.write(report)

    if Config.SIMULATE is not True and (input_vars.get('addedcnt', 0) > 0 or input_vars.get('liftedcnt', 0) > 0):
        report_page = pwb.Page(Config.WIKIDATA_SITE, Config.REPORT_PAGE)
        report_page.text = report
        report_page.save(
            summary=Config.REPORT_EDITSUMMARY,
            watch='nochange',
            minor=True,
            quiet=True
        )


def main() -> None:
    # gather input data
    indef_semiprotected_items = get_indefinitely_semiprotected_items() # all currently indefinitely semi-protected items
    early_item_protections = get_early_item_protections() # all items protected under the "highly used" scheme, but not by User:MsynABot
    wdcm_toplist = get_list_of_highly_used_items_from_wdcm() # list of highly used items, compiled weekly by WMDE
    blacklist = get_blacklisted_items() # onwiki list of items that should not be protected (Tour items, sandbox, etc)
    total_number_of_items = get_number_of_items() # total number of ns0 non-redirects

    # compute auxiliary variables
    indef_protected_highly_used_items = protected_highly_used_items(indef_semiprotected_items, early_item_protections)
    indef_protected_not_highly_used_items = protected_not_highly_used_items(indef_semiprotected_items, early_item_protections)

    item_protection_to_add = protection_missing(wdcm_toplist, indef_semiprotected_items)
    item_protection_to_lift = protection_to_lift(wdcm_toplist, indef_protected_highly_used_items)
    item_protection_in_cooldown = protection_in_cooldown(wdcm_toplist, indef_protected_highly_used_items)

    highly_used_items_already_protected_for_other_reasons = protection_already_set(wdcm_toplist, indef_protected_not_highly_used_items)

    # lift protections
    if Config.LIFTLIMIT is None or item_protection_to_lift.shape[0] <= Config.LIFTLIMIT:
        for processed, case in enumerate(item_protection_to_lift.itertuples(index=False), start=1):
            early_protection = early_item_protections[early_item_protections['qid'] == case.qid] # this is a pandas dataframe; empty if qid not found
            try:
                msg = remove_protection(case, early_protection)
            except RuntimeWarning as exception:
                print(exception)
            except RuntimeError as exception:
                print(exception)
            else:
                print(msg)

            if Config.HARDLIMIT is not None and processed >= Config.HARDLIMIT:
                break
    else:
        print(f'Do not lift any protections, as the list has {item_protection_to_lift.shape[0]} entries (limit: {Config.LIFTLIMIT})')

    # add protections
    if Config.ADDLIMIT is None or item_protection_to_add.shape[0] <= Config.ADDLIMIT:
        for processed, case in enumerate(item_protection_to_add.itertuples(index=False), start=1):
            try:
                msg = add_protection(case, blacklist)
            except RuntimeWarning as exception:
                print(exception)
            except RuntimeError as exception:
                print(exception)
            else:
                print(msg)

            if Config.HARDLIMIT is not None and processed >= Config.HARDLIMIT:
                break
    else:
        print(f'Do not add any protections, as the list has {item_protection_to_add.shape[0]} entries (limit: {Config.ADDLIMIT})')

    # reporting to terminal/log file
    print(f'Number of early item protections: {early_item_protections.shape[0]}')
    print(f'Number of elements in WDCM toplist: {wdcm_toplist.shape[0]}')
    print('Number of elements in WDCM toplist (cnt>={limit}): {cnt} ({perc:.4f}% of {total} items)'.format(
        limit=Config.ENTITYUSAGELIMIT,
        cnt=wdcm_toplist.loc[wdcm_toplist['entityUsageCount']>=Config.ENTITYUSAGELIMIT].shape[0],
        perc=wdcm_toplist.loc[wdcm_toplist['entityUsageCount']>=Config.ENTITYUSAGELIMIT].shape[0] / total_number_of_items * 100,
        total=total_number_of_items
    ))
    print(f'Number of blacklisted items: {len(blacklist)}')

    print(f'Number of indef semiprotected items: {indef_semiprotected_items.shape[0]}')
    print(f'Number of indef semiprotected items (highly used): {indef_protected_highly_used_items.shape[0]}')
    print(f'Number of indef semiprotected items (other): {indef_protected_not_highly_used_items.shape[0]}')
    print(f'Number of indef semiprotected items (other, but also highly used): {highly_used_items_already_protected_for_other_reasons.shape[0]}')

    print(f'Number of protections to add: {item_protection_to_add.shape[0]}')
    print(f'Number of protections to lift: {item_protection_to_lift.shape[0]}')
    print(f'Number of protections not to lift (cooldown): {item_protection_in_cooldown.shape[0]}')

    print(f'Number of protections added in this run: {Counter.added_protection["successful"]["cnt"]}')
    print(f'Number of protections lifted in this run: {Counter.removed_protection["successful"]["cnt"]}')

    # reporting to an onwiki page
    make_report({
        'timestmp' : strftime('%Y-%m-%d, %H:%M:%S', Config.SCRIPTRUNTIME),
        'wdcmurl' : Config.WDCM_TOPLIST_URL,
        'blacklisturl' : Config.BLACKLIST_URL,
        'entityusagelimit' : Config.ENTITYUSAGELIMIT,
        'cooldownlimit' : Config.COOLDOWNUSAGELIMIT,
        'addlimit' : Config.ADDLIMIT,
        'liftlimit' : Config.LIFTLIMIT,
        'hardlimit' : Config.HARDLIMIT,
        'minsubscribedprojects' : Config.MINSUBSCRIBEDPROJECTS,
        'wdcmcnt' : wdcm_toplist.shape[0],
        'wdcmusagelimit' : wdcm_toplist.loc[wdcm_toplist['entityUsageCount']>=Config.ENTITYUSAGELIMIT].shape[0],
        'wdcmpercent' : wdcm_toplist.loc[wdcm_toplist['entityUsageCount']>=Config.ENTITYUSAGELIMIT].shape[0] / total_number_of_items * 100,
        'itemcnt' : total_number_of_items,
        'blacklistedcnt' : len(blacklist),
        'indefsemi' : indef_semiprotected_items.shape[0],
        'indefsemihighlyused' : indef_protected_highly_used_items.shape[0],
        'indefsemiother' : indef_protected_not_highly_used_items.shape[0],
        'indefsemiotherbutalsohighlyused' : highly_used_items_already_protected_for_other_reasons.shape[0],
        'protectionstoadd' : item_protection_to_add.shape[0],
        'protectionstolift' : item_protection_to_lift.shape[0],
        'cooldowncnt' : item_protection_in_cooldown.shape[0],
        'cooldownlist' : '{{Q|' + '}}, {{Q|'.join(item_protection_in_cooldown['qid'].tolist()) + '}}',
        'addedcnt' : Counter.added_protection['successful']['cnt'],
        'liftedcnt' : Counter.removed_protection['successful']['cnt'],
        'additionstats' : Counter.make_table(Counter.added_protection),
        'removalstats' : Counter.make_table(Counter.removed_protection)
    })


if __name__ == '__main__':
    sys.stdout = Logger(
        write_to_terminal=Config.WRITETOTERMINAL,
        write_to_log=Config.WRITETOLOG
    )
    main()
