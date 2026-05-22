import datetime
import logging
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

from src.data.cache import get_cache
from src.data.models import (
    CompanyNews,
    FinancialMetrics,
    Price,
    LineItem,
    InsiderTrade,
)

# Global cache instance
_cache = get_cache()


def get_prices(ticker: str, start_date: str, end_date: str) -> list[Price]:
    """Fetch price data from cache or API."""
    # Create a cache key that includes all parameters to ensure exact matches
    cache_key = f"{ticker}_{start_date}_{end_date}"
    
    # Check cache first - simple exact match
    if cached_data := _cache.get_prices(cache_key):
        return [Price(**price) for price in cached_data]

    # If not in cache, fetch from yfinance
    try:
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(start=start_date, end=end_date)
    except Exception as e:
        logger.warning(f"Failed to fetch price data for {ticker} from yfinance: {e}")
        return []

    if df.empty:
        return []

    df.reset_index(inplace=True)
    df.rename(columns={
        "Date": "time",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume"
    }, inplace=True)

    prices = [
        Price(
            ticker=ticker,
            time=row["time"].strftime("%Y-%m-%dT%H:%M:%S"),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )
        for _, row in df.iterrows()
    ]

    # Cache the results using the comprehensive cache key
    _cache.set_prices(cache_key, [p.model_dump() for p in prices])
    return prices


def get_financial_metrics(
    ticker: str,
    end_date: str,
    period: str = "quarterly",
    limit: int = 10,
) -> list[FinancialMetrics]:
    """Fetch financial metrics from cache or API."""
    cache_key = f"{ticker}_{period}_{end_date}_{limit}"
    
    if cached_data := _cache.get_financial_metrics(cache_key):
        return [FinancialMetrics(**metric) for metric in cached_data]

    try:
        ticker_obj = yf.Ticker(ticker)
        if period == "quarterly":
            financials = ticker_obj.quarterly_financials
            balance_sheet = ticker_obj.quarterly_balance_sheet
        else: # ttm/annual
            financials = ticker_obj.financials
            balance_sheet = ticker_obj.balance_sheet
            
        info = ticker_obj.info
    except Exception as e:
        logger.warning(f"Failed to fetch financial data for {ticker} from yfinance: {e}")
        return []

    if financials.empty or balance_sheet.empty:
        return []
        
    # Ensure columns are sorted in descending order (newest first)
    try:
        financials = financials[sorted(financials.columns, reverse=True)]
    except Exception:
        pass

    financial_metrics = []
    for report_date in financials.columns[:limit]:
        report_date_str = report_date.strftime("%Y-%m-%d")
        if report_date_str > end_date:
            continue

        income_statement = financials[report_date]
        balance_sheet_data = balance_sheet[report_date] if report_date in balance_sheet.columns else None

        if balance_sheet_data is None:
            continue
            
        market_cap = info.get("marketCap")
        enterprise_value = info.get("enterpriseValue")
        
        # Calculate metrics
        total_revenue = income_statement.get("Total Revenue", 0)
        net_income = income_statement.get("Net Income", 0)
        total_assets = balance_sheet_data.get("Total Assets")
        total_liabilities = balance_sheet_data.get("Total Liab")
        total_debt = balance_sheet_data.get("Total Debt", balance_sheet_data.get("Total Liab")) # Fallback for Total Debt
        
        # Ensure values are valid before performing calculations (handle None and NaN)
        def is_valid(val):
            return val is not None and not pd.isna(val)

        if not all(is_valid(v) for v in [total_assets, total_liabilities, total_debt, total_revenue]):
            continue
            
        debt_to_equity = total_debt / (total_assets - total_liabilities) if (total_assets - total_liabilities) != 0 else 0
        
        current_assets = balance_sheet_data.get("Current Assets")
        current_liabilities = balance_sheet_data.get("Current Liabilities")
        if is_valid(current_assets) and is_valid(current_liabilities) and current_liabilities != 0:
            current_ratio = current_assets / current_liabilities
        else:
            current_ratio = 0.0
            
        pe_ratio = market_cap / net_income if is_valid(market_cap) and is_valid(net_income) and net_income > 0 else None
        ps_ratio = market_cap / total_revenue if is_valid(market_cap) and is_valid(total_revenue) and total_revenue > 0 else None
        pb_ratio = market_cap / (total_assets - total_liabilities) if is_valid(market_cap) and (total_assets - total_liabilities) != 0 else None
        
        ebitda = info.get("ebitda")
        ev_to_ebitda = enterprise_value / ebitda if is_valid(enterprise_value) and is_valid(ebitda) and ebitda > 0 else None
        
        metrics = FinancialMetrics(
            ticker=ticker,
            report_date=report_date_str,
            period=period,
            market_cap=market_cap,
            enterprise_value=enterprise_value,
            revenue=total_revenue,
            net_income=net_income,
            debt_to_equity=debt_to_equity,
            current_ratio=current_ratio,
            pe_ratio=pe_ratio,
            ps_ratio=ps_ratio,
            pb_ratio=pb_ratio,
            ev_to_ebitda=ev_to_ebitda,
        )
        financial_metrics.append(metrics)

    _cache.set_financial_metrics(cache_key, [m.model_dump() for m in financial_metrics])
    return financial_metrics


def search_line_items(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "quarterly",
    limit: int = 10,
) -> list[LineItem]:
    """Fetch line items from yfinance and return as a list of periods with requested attributes."""
    try:
        ticker_obj = yf.Ticker(ticker)
        if period == "quarterly":
            financials = ticker_obj.quarterly_financials
            balance_sheet = ticker_obj.quarterly_balance_sheet
            cash_flow = ticker_obj.quarterly_cashflow
        else: # ttm/annual
            financials = ticker_obj.financials
            balance_sheet = ticker_obj.balance_sheet
            cash_flow = ticker_obj.cashflow
    except Exception as e:
        logger.warning(f"Failed to fetch financial data for {ticker} from yfinance: {e}")
        return []

    # Combine all statement types to pull any requested line item
    combined_df = pd.concat([financials, balance_sheet, cash_flow])
    # Remove duplicate indices if they exist across statements
    combined_df = combined_df[~combined_df.index.duplicated(keep='first')]

    # Ensure columns are sorted in descending order (newest first)
    try:
        combined_df = combined_df[sorted(combined_df.columns, reverse=True)]
    except Exception:
        pass

    # Fallback to ticker.info if historical statements are empty
    if combined_df.empty:
        logger.warning(f"No historical statements for {ticker}. Falling back to ticker.info for a single data point.")
        try:
            info = ticker_obj.info
            if not info.get("marketCap"): # Check if info has meaningful data
                return []
        except Exception as e:
            logger.warning(f"Could not fetch ticker.info for {ticker}: {e}")
            return []

        # Mapping from our snake_case to yfinance info dict keys
        info_mapping = {
            "revenue": "totalRevenue",
            "net_income": "netIncomeToCommon",
            "earnings_per_share": "trailingEps",
            "free_cash_flow": "freeCashflow",
            "cash_and_equivalents": "totalCash",
            "total_debt": "totalDebt",
            "outstanding_shares": "sharesOutstanding",
            "ebitda": "ebitda",
            "operating_margin": "operatingMargins",
            "gross_margin": "grossMargins",
            "debt_to_equity": "debtToEquity",
            "return_on_invested_capital": "returnOnEquity", # Proxy
        }
        
        period_data = {
            "ticker": ticker,
            "report_date": datetime.datetime.now().strftime("%Y-%m-%d")
        }
        
        for item in line_items:
            info_key = info_mapping.get(item)
            val = None
            if info_key and info_key in info:
                val = info.get(info_key)
            
            # Special calculations for missing direct values
            if val is None:
                if item == "shareholders_equity":
                    book_value_ps = info.get("bookValue")
                    shares_outstanding = info.get("sharesOutstanding")
                    if book_value_ps and shares_outstanding:
                        val = book_value_ps * shares_outstanding
                elif item == "debt_to_equity":
                    total_debt = info.get("totalDebt")
                    book_value_ps = info.get("bookValue")
                    shares_outstanding = info.get("sharesOutstanding")
                    if total_debt is not None and book_value_ps and shares_outstanding and book_value_ps > 0 and shares_outstanding > 0:
                        shareholder_equity = book_value_ps * shares_outstanding
                        if shareholder_equity > 0:
                            val = total_debt / shareholder_equity
            
            period_data[item] = val if (val is not None and not pd.isna(val)) else None
        return [LineItem(**period_data)]

    # Map agent-requested snake_case keys to yfinance index names
    yfinance_mapping = {
        "revenue": "Total Revenue",
        "net_income": "Net Income",
        "earnings_per_share": "Basic EPS",
        "operating_income": "Operating Income",
        "free_cash_flow": "Free Cash Flow",
        "capital_expenditure": "Capital Expenditure",
        "cash_and_equivalents": "Cash And Cash Equivalents",
        "total_debt": "Total Debt",
        "shareholders_equity": "Stockholders Equity",
        "outstanding_shares": "Ordinary Shares Number",
        "ebit": "EBIT",
        "ebitda": "EBITDA",
        "interest_expense": "Interest Expense",
        "depreciation_and_amortization": "Reconciled Depreciation",
        "total_assets": "Total Assets",
        "total_liabilities": "Total Liabilities",
        "current_assets": "Current Assets",
        "current_liabilities": "Current Liabilities",
        "dividends_and_other_cash_distributions": "Cash Dividends Paid",
        "issuance_or_purchase_of_equity_shares": "Repurchase Of Capital Stock",
        "working_capital": "Working Capital",
        "research_and_development": "Research And Development",
        "goodwill_and_intangible_assets": "Goodwill And Other Intangible Assets",
        "operating_expense": "Operating Expense",
    }

    results = []
    for report_date in combined_df.columns[:limit]:
        report_date_str = report_date.strftime("%Y-%m-%d")
        if report_date_str > end_date:
            continue
            
        period_data = {
            "ticker": ticker,
            "report_date": report_date_str
        }
        
        for item in line_items:
            yf_name = yfinance_mapping.get(item)
            val = None
            if yf_name and yf_name in combined_df.index:
                val = combined_df.loc[yf_name, report_date]
                val = None if pd.isna(val) else float(val)
            
            # Custom fallbacks for common alternative names in yfinance
            if val is None:
                if item == "total_liabilities" and "Total Liab" in combined_df.index:
                    val = combined_df.loc["Total Liab", report_date]
                    val = None if pd.isna(val) else float(val)

            # Handle computed margins if raw data exists but ratio doesn't
            if val is None:
                if item == "gross_margin" and "Gross Profit" in combined_df.index and "Total Revenue" in combined_df.index:
                    rev = combined_df.loc["Total Revenue", report_date]
                    gp = combined_df.loc["Gross Profit", report_date]
                    if rev and gp and not pd.isna(rev) and not pd.isna(gp) and rev != 0:
                        val = float(gp / rev)
                elif item == "operating_margin" and "Operating Income" in combined_df.index and "Total Revenue" in combined_df.index:
                    rev = combined_df.loc["Total Revenue", report_date]
                    op = combined_df.loc["Operating Income", report_date]
                    if rev and op and not pd.isna(rev) and not pd.isna(op) and rev != 0:
                        val = float(op / rev)

            period_data[item] = val
            
        results.append(LineItem(**period_data))

    return results


def get_insider_trades(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
) -> list[InsiderTrade]:
    """Insider trade data is not available via yfinance. Returning empty list."""
    logger.warning("Insider trade data is not available via yfinance.")
    return []


def get_company_news(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 100,
) -> list[CompanyNews]:
    """Fetch company news from cache or yfinance."""
    cache_key = f"{ticker}_{start_date or 'none'}_{end_date}_{limit}"
    
    if cached_data := _cache.get_company_news(cache_key):
        return [CompanyNews(**news) for news in cached_data]

    try:
        ticker_obj = yf.Ticker(ticker)
        news = ticker_obj.news
    except Exception as e:
        logger.warning(f"Failed to fetch news for {ticker} from yfinance: {e}")
        return []

    if not news:
        return []

    all_news = []
    for item in news:
        timestamp = item.get("providerPublishTime")
        if timestamp is None:
            continue
            
        publish_time = datetime.datetime.fromtimestamp(timestamp)
        
        # Apply date filters if provided
        if start_date and publish_time < datetime.datetime.strptime(start_date, "%Y-%m-%d"):
            continue
        if end_date and publish_time > datetime.datetime.strptime(end_date, "%Y-%m-%d"):
            continue

        all_news.append(
            CompanyNews(
                ticker=ticker,
                title=item.get("title", ""),
                url=item.get("link", ""),
                source=item.get("publisher", ""),
                date=publish_time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        )
        if len(all_news) >= limit:
            break

    _cache.set_company_news(cache_key, [n.model_dump() for n in all_news])
    return all_news


def get_market_cap(
    ticker: str,
    end_date: str, # end_date is not used with yfinance for current market cap
) -> float | None:
    """Fetch market cap from yfinance."""
    try:
        ticker_obj = yf.Ticker(ticker)
        market_cap = ticker_obj.info.get("marketCap")
    except Exception as e:
        logger.warning(f"Failed to fetch market cap for {ticker} from yfinance: {e}")
        return None
        
    return market_cap


def prices_to_df(prices: list[Price]) -> pd.DataFrame:
    """Convert prices to a DataFrame."""
    df = pd.DataFrame([p.model_dump() for p in prices])
    if not df.empty:
        df["Date"] = pd.to_datetime(df["time"])
        df.set_index("Date", inplace=True)
        numeric_cols = ["open", "close", "high", "low", "volume"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.sort_index(inplace=True)
    return df


def get_price_data(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    prices = get_prices(ticker, start_date, end_date)
    return prices_to_df(prices)
