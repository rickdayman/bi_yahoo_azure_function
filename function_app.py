import azure.functions as func
import logging

import yfinance as yf
import pandas as pd
import numpy as np
from azure.storage.blob import BlobServiceClient
import pyodbc
from sqlalchemy import create_engine
from io import StringIO
from datetime import datetime
import urllib
import os

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="http_trigger_az_get_yahoo_data")
def http_trigger_az_get_yahoo_data(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    #################################################################################################
    ## Get environmental variables
    #################################################################################################   

    # Get Azure Blob Storage account details
    account_name = 'azrickdaymanstorage'
    container_name = 'azrickdaymanyahoo'
    blob_name = 'stock_tickers_top_10_test.csv'
    account_key = os.environ["BLOB_ACCOUNT_KEY"]

    #################################################################################################
    ## Connect to azure blob storage - download tickers csv
    #################################################################################################

    # connect to blob
    blob_service_client = BlobServiceClient(account_url=f"https://{account_name}.blob.core.windows.net", credential=account_key)
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
    downloaded_csv = blob_client.download_blob(encoding='utf8')

    # load csv from blob into dataframe
    tickers_list = pd.read_csv(StringIO(downloaded_csv.readall()))

    #################################################################################################
    # get daily ohlc data from yahoo
    #################################################################################################

    startdate = '2019-12-31'
    fx_gold_oil = ['GC=F','GBPUSD=X','EURUSD=X','USDJPY=X', 'CL=F', '^GSPC', 'BTC-USD']
    symbols = tickers_list['symbol'].to_list()
    symbols = symbols + fx_gold_oil

    df_data = pd.DataFrame(yf.download(symbols, start = startdate, threads = True, proxy = None, group_by = 'ticker'))

    df_data = df_data.stack(level=0, future_stack = True).rename_axis(['Date', 'Ticker']).reset_index()
    df_data['Date'] = pd.to_datetime(df_data['Date'], format = '%Y-%m-%d').dt.tz_localize(None)
    df_data.rename(columns={'Adj Close': 'adj_close'}, inplace = True)
    df_data.columns = [col.lower() for col in df_data]
    df_data = df_data.sort_values(['ticker', 'date'], ascending = [True, True])

    df_data.dropna(how='any', inplace=True)

    #del df_data['adj_close']
    del df_data['volume']

    df_data['previous_close'] = df_data['close'].shift(1, fill_value=0)
    df_data['previous_close'] = np.where((df_data['previous_close'] == 0), df_data['close'], df_data['previous_close'])
    df_data['percent_increase'] = df_data.apply(lambda x: (x.close - x.previous_close) / x.previous_close, axis=1)
    df_data['percent_increase_multipler'] = df_data.apply(lambda x: 1 + ((x.close - x.previous_close) / x.previous_close), axis=1)

    date_filter = datetime.strptime(startdate, '%Y-%m-%d')
    df_data = df_data[df_data.date != date_filter]

    #################################################################################################
    ## Connect to azure blob storage - upload csv
    #################################################################################################

    # format dataframe results to load to blob
    blob_name = 'dailystockprices.csv'
    csv_string = df_data.to_csv(index = False)
    csv_bytes = str.encode(csv_string)

    # Upload the bytes object to Azure Blob Storage
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
    blob_client.upload_blob(csv_bytes, overwrite=True)     

    #################################################################################################
    ## Load data from yahoo into dataframe
    #################################################################################################

    date_recent = datetime.strptime('2024-10-01', '%Y-%m-%d')
    df_recent_tickers = df_data[df_data.date >= date_recent]
    symbols = df_recent_tickers.ticker.unique()

    allholders = pd.DataFrame()
    allinfo = pd.DataFrame()
    symbols = [x for x in symbols if x not in fx_gold_oil]
    symbol_count = len(symbols)
    symbol_counter = 0

    for symbol in symbols:
        try:
            stock = yf.Ticker(symbol)

            holders = pd.DataFrame(stock.institutional_holders)
            holders['symbol'] = symbol

            info = pd.DataFrame(index = stock.info, data = stock.info.values(), columns = [stock.info['symbol']])
            info = info.T

            allholders = pd.concat([allholders, holders])
            allinfo = pd.concat([allinfo, info])

        except:
            print("Error on ticker - " + symbol)
            continue

        # symbol_counter = symbol_counter + 1
        # percent_bucket = int(symbol_count / 10)
        # if(symbol_counter % percent_bucket == 0):
        #     print_time = datetime.now().strftime("%d/%m/%Y, %H:%M:%S")
        #     print(print_time + ': ' + str(int(symbol_counter / percent_bucket * 10)) + ' % complete')

    allinfo = allinfo.reset_index()

    #################################################################################################
    ## Add data to Azure SQL server
    #################################################################################################

    # Get Azure SQL details
    server = 'azrickdaymanserver.database.windows.net'
    database = 'YAHOO'
    username = os.environ["DATABASE_YAHOO_USERNAME"]
    password = os.environ["DATABASE_YAHOO_PASSWORD"]
    #driver= '{ODBC Driver 18 for SQL Server}'
    driver= '{ODBC Driver 17 for SQL Server}' # to run locally

    try:
        quoted = urllib.parse.quote_plus('DRIVER='+driver+';SERVER=tcp:'+server+';PORT=1433;DATABASE='+database+';UID='+username+';PWD='+ password + ';Encrypt=no;TrustServerCertificate=no;')
        engine = create_engine('mssql+pyodbc:///?odbc_connect={}'.format(quoted), fast_executemany=True)

        allholders.to_sql("yfinance_institutional_investors", schema="stg", con=engine, index=True, if_exists='replace', method='multi', chunksize=250)
        allinfo.to_sql("yfinance_stock_information", schema="stg", con=engine, index=True, if_exists='replace', method='multi', chunksize=10)
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return func.HttpResponse(f"An error occurred: {e}", status_code=500)

    #################################################################################################
    ## End code
    #################################################################################################

    return func.HttpResponse(
                            "This HTTP triggered function executed successfully",
                            status_code=200
                            )