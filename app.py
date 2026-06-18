from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


st.set_page_config(page_title="밸류에이션 밴드", layout="wide")


def blank_stock(code: str = "NEW-1") -> dict[str, str]:
    return {
        "name": "새 종목",
        "code": code,
        "mode": "PER",
        "basis": "확정 실적",
        "perBands": "8, 10, 12, 15, 20",
        "pbrBands": "0.5, 1, 1.5, 2.0, 2.5",
        "prices": "",
        "actuals": "",
        "forecast": "",
    }


def starter_db() -> dict[str, Any]:
    stock = blank_stock()
    return {"active": stock["code"], "stocks": {stock["code"]: stock}}


def api_base_from_config() -> str:
    secret_value = st.secrets.get("ORACLE_API_BASE", "") if hasattr(st, "secrets") else ""
    default_value = secret_value or "http://YOUR_ORACLE_IP"
    value = st.sidebar.text_input("Oracle API 주소", value=default_value)
    return value.rstrip("/")


def api_get(base: str, path: str, **params: Any) -> dict[str, Any]:
    response = requests.get(f"{base}{path}", params=params, timeout=40)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


def api_post(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(f"{base}{path}", json=payload, timeout=40)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def load_db(base: str) -> dict[str, Any]:
    try:
        payload = api_get(base, "/api/workbench")
        if payload.get("stocks"):
            return payload
    except Exception as exc:
        st.sidebar.warning(f"서버 저장값을 불러오지 못했습니다: {exc}")
    return starter_db()


def save_db(base: str, db: dict[str, Any]) -> None:
    api_post(base, "/api/save-workbench", db)


def refresh_prices_if_stale(base: str, db: dict[str, Any], stock: dict[str, Any], code: str) -> None:
    clean_code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(clean_code) != 6:
        return

    refresh_key = f"price_refresh:{clean_code}:{date.today().isoformat()}"
    if st.session_state.get(refresh_key):
        return

    current_prices = parse_rows(stock.get("prices", ""), "price")
    latest_saved = current_prices["date"].max().date() if not current_prices.empty else None
    if latest_saved and latest_saved >= date.today():
        st.session_state[refresh_key] = True
        return

    try:
        payload = api_get(base, "/api/load-stock", code=clean_code)
        prices = payload.get("prices", [])
        if prices:
            stock["prices"] = serialize_rows(prices, "price")
            save_db(base, db)
        st.session_state[refresh_key] = True
    except Exception as exc:
        st.sidebar.caption(f"최신 주가 자동 갱신 실패: {exc}")
        st.session_state[refresh_key] = True


def to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "N/A", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.replace("/", "-").replace(".", "-").split("-")
    if len(parts) == 1 and len(parts[0]) == 4:
        return f"{parts[0]}-12-31"
    if len(parts) == 2:
        return f"{parts[0]}-{parts[1].zfill(2)}-31"
    if len(parts) >= 3:
        return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    return text


def numbers_from_line(line: str) -> list[float]:
    import re

    values = []
    for match in re.finditer(r"[-(]?\d[\d,]*\.?\d*\)?", line):
        number = to_number(match.group(0).replace("(", "-").replace(")", ""))
        if number is not None:
            values.append(number)
    return values


def parse_rows(text: str, kind: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        import re

        date_match = re.match(r"^\s*(20\d{2}(?:[./-]\d{1,2}(?:[./-]\d{1,2})?)?)", line)
        raw_date = date_match.group(1) if date_match else line.split(",")[0]
        rest = line[len(date_match.group(0)) :] if date_match else line[len(raw_date) :]
        nums = numbers_from_line(rest)
        if kind == "price" and nums:
            rows.append({"date": normalize_date(raw_date), "price": nums[0]})
        elif kind == "actual" and len(nums) >= 2:
            rows.append({"date": normalize_date(raw_date), "eps": nums[0], "bps": nums[1]})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    return df


def serialize_rows(rows: list[dict[str, Any]], kind: str) -> str:
    lines = []
    for row in rows:
        if kind == "price":
            lines.append(f"{row['date']}, {row['price']}")
        else:
            lines.append(f"{row['date']}, {row['eps']}, {row['bps']}")
    return "\n".join(lines)


def parse_bands(text: str) -> list[float]:
    values = [to_number(x) for x in text.split(",")]
    return sorted([float(v) for v in values if v and v > 0])


def latest_actual(actuals: pd.DataFrame, at_date: pd.Timestamp) -> pd.Series | None:
    if actuals.empty:
        return None
    subset = actuals[actuals["date"] <= at_date]
    if subset.empty:
        return None
    return subset.iloc[-1]


def band_value(row: pd.Series, multiple: float, mode: str) -> float:
    return float(row["eps"] if mode == "PER" else row["bps"]) * multiple


def metric_column(mode: str) -> str:
    return "eps" if mode == "PER" else "bps"


def interpolated_metric(actuals: pd.DataFrame, dates: pd.Series, mode: str) -> pd.DataFrame:
    column = metric_column(mode)
    empty = pd.DataFrame(columns=["date", "metric"])
    if actuals.empty or dates.empty or column not in actuals.columns:
        return empty

    metric_points = (
        actuals[["date", column]]
        .dropna()
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .rename(columns={column: "metric"})
    )
    if metric_points.empty:
        return empty

    target_dates = pd.to_datetime(pd.Series(dates).dropna().drop_duplicates()).sort_values()
    if target_dates.empty:
        return empty

    series = metric_points.set_index("date")["metric"].astype(float)
    combined_index = series.index.union(pd.DatetimeIndex(target_dates))
    interpolated = series.reindex(combined_index).sort_index().interpolate(method="time").ffill().bfill()
    values = interpolated.reindex(pd.DatetimeIndex(target_dates))
    return pd.DataFrame({"date": target_dates.to_list(), "metric": values.to_list()}).dropna()


def metric_source(actuals: pd.DataFrame, forecast: pd.DataFrame, basis: str) -> pd.DataFrame:
    columns = ["date", "eps", "bps"]
    if basis == "확정 실적":
        return actuals if not actuals.empty else pd.DataFrame(columns=columns)

    frames = []
    if not actuals.empty:
        frames.append(actuals[columns])
    if not forecast.empty:
        frames.append(forecast[columns])
    if not frames:
        return pd.DataFrame(columns=columns)
    return (
        pd.concat(frames, ignore_index=True)
        .dropna(subset=["date"])
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
    )


def valuation_metric(
    actuals: pd.DataFrame,
    forecast: pd.DataFrame,
    dates: pd.Series,
    mode: str,
    basis: str,
) -> pd.DataFrame:
    if basis == "12M Forward":
        source = metric_source(actuals, forecast, "예상 실적")
        shifted_dates = pd.to_datetime(dates) + pd.DateOffset(years=1)
        forward = interpolated_metric(source, shifted_dates, mode)
        if forward.empty:
            return forward
        return pd.DataFrame({"date": pd.to_datetime(dates).to_list(), "metric": forward["metric"].to_list()}).dropna()

    return interpolated_metric(metric_source(actuals, forecast, basis), dates, mode)


def build_chart(
    prices: pd.DataFrame,
    actuals: pd.DataFrame,
    forecast: pd.DataFrame,
    mode: str,
    basis: str,
    bands: list[float],
    start_date: date | None,
    end_date: date | None,
) -> tuple[go.Figure, pd.DataFrame]:
    all_prices = prices.copy()
    if not prices.empty and start_date:
        prices = prices[prices["date"] >= pd.Timestamp(start_date)]
    if not prices.empty and end_date:
        prices = prices[prices["date"] <= pd.Timestamp(end_date)]

    last_price_date = all_prices["date"].max() if not all_prices.empty else pd.Timestamp("1900-01-01")
    future_columns = ["date", "eps", "bps"]
    future_all = (
        forecast[forecast["date"] > last_price_date].copy()
        if not forecast.empty and "date" in forecast.columns
        else pd.DataFrame(columns=future_columns)
    )
    future_chart = future_all.copy()
    if "date" not in future_chart.columns:
        future_chart = pd.DataFrame(columns=future_columns)
    if not future_chart.empty and start_date:
        future_chart = future_chart[future_chart["date"] >= pd.Timestamp(start_date)]
    if not future_chart.empty and end_date:
        future_chart = future_chart[future_chart["date"] <= pd.Timestamp(end_date)]

    fig = go.Figure()
    if not prices.empty:
        fig.add_trace(
            go.Scatter(
                x=prices["date"],
                y=prices["price"],
                mode="lines",
                name="종가",
                line=dict(color="#111827", width=3),
            )
        )

    historical_metric = valuation_metric(
        actuals,
        forecast,
        prices["date"] if "date" in prices.columns else pd.Series([], dtype="datetime64[ns]"),
        mode,
        basis,
    )
    for idx, multiple in enumerate(bands):
        if not historical_metric.empty:
            fig.add_trace(
                go.Scatter(
                    x=historical_metric["date"],
                    y=historical_metric["metric"] * multiple,
                    mode="lines",
                    name=f"{multiple:g}x",
                    line=dict(width=1.8),
                )
            )
        if not future_chart.empty:
            future_points = valuation_metric(actuals, forecast, future_chart["date"], mode, basis)
            if not historical_metric.empty:
                future_points = pd.concat([historical_metric.tail(1), future_points], ignore_index=True)
            y_values = future_points["metric"] * multiple
            fig.add_trace(
                go.Scatter(
                    x=future_points["date"],
                    y=y_values,
                    mode="lines+markers",
                    name=f"{multiple:g}x 예상",
                    line=dict(width=2, dash="dash"),
                    showlegend=False,
                )
            )

    fig.update_layout(
        height=620,
        margin=dict(l=20, r=20, t=30, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
        hovermode="x unified",
        paper_bgcolor="white",
        plot_bgcolor="white",
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="#e5e9f0", tickformat=",")
    return fig, future_all


def ensure_state(base: str) -> None:
    if "api_base" not in st.session_state or st.session_state.api_base != base:
        st.session_state.api_base = base
        st.session_state.db = load_db(base)
    if "db" not in st.session_state:
        st.session_state.db = starter_db()


def active_stock(db: dict[str, Any]) -> dict[str, str]:
    if not db.get("stocks"):
        db.update(starter_db())
    active = db.get("active")
    return db["stocks"].get(active) or next(iter(db["stocks"].values()))


def sync_stock(stock: dict[str, str], values: dict[str, str]) -> None:
    stock.update(values)


def main() -> None:
    st.title("밸류에이션 밴드")
    st.caption("Streamlit 화면은 Oracle 서버의 저장 데이터와 수집 API를 사용합니다.")

    base = api_base_from_config()
    ensure_state(base)
    db = st.session_state.db
    stock = active_stock(db)

    with st.sidebar:
        st.header("종목")
        options = {
            f"{item.get('name', '종목')} ({item.get('code', '')})": code
            for code, item in db.get("stocks", {}).items()
        }
        labels = list(options.keys())
        current_label = next((label for label, code in options.items() if code == db.get("active")), labels[0])
        selected_label = st.selectbox("저장된 종목", labels, index=labels.index(current_label))
        if options[selected_label] != db.get("active"):
            db["active"] = options[selected_label]
            st.rerun()

        name = st.text_input("종목명", value=stock.get("name", ""))
        code = st.text_input("종목코드", value=stock.get("code", ""))
        mode = st.selectbox("차트 지표", ["PER", "PBR"], index=0 if stock.get("mode", "PER") == "PER" else 1)
        basis_options = ["확정 실적", "예상 실적", "12M Forward"]
        basis = st.selectbox(
            "밴드 실적 기준",
            basis_options,
            index=basis_options.index(stock.get("basis", "확정 실적"))
            if stock.get("basis", "확정 실적") in basis_options
            else 0,
        )
        per_bands = st.text_input("PER 배수", value=stock.get("perBands", "8, 10, 12, 15, 20"))
        pbr_bands = st.text_input("PBR 배수", value=stock.get("pbrBands", "0.5, 1, 1.5, 2.0, 2.5"))

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("새 종목", use_container_width=True):
                new_code = f"NEW-{len(db['stocks']) + 1}"
                db["stocks"][new_code] = blank_stock(new_code)
                db["active"] = new_code
                st.rerun()
        with col_b:
            if st.button("삭제", use_container_width=True):
                db["stocks"].pop(db.get("active"), None)
                if not db["stocks"]:
                    st.session_state.db = starter_db()
                else:
                    db["active"] = next(iter(db["stocks"]))
                st.rerun()

        if st.button("직접 가져오기", type="primary", use_container_width=True):
            try:
                payload = api_get(base, "/api/load-stock", code=code)
                stock["name"] = payload.get("name") or name
                stock["code"] = payload.get("code") or code
                stock["actuals"] = serialize_rows(payload.get("actuals", []), "actual")
                stock["forecast"] = serialize_rows(payload.get("forecasts", []), "actual")
                stock["prices"] = serialize_rows(payload.get("prices", []), "price")
                save_db(base, db)
                st.success("서버에서 실적과 일별 주가를 가져왔습니다.")
                st.rerun()
            except Exception as exc:
                st.error(f"직접 가져오기 실패: {exc}")

        refresh_prices_if_stale(base, db, stock, code)

        st.divider()
        prices_text = st.text_area("주가: date, price", value=stock.get("prices", ""), height=140)
        actuals_text = st.text_area("과거 실적: date, eps, bps", value=stock.get("actuals", ""), height=170)
        forecast_text = st.text_area("미래 실적: date, eps, bps", value=stock.get("forecast", ""), height=120)

        if st.button("저장", use_container_width=True):
            sync_stock(
                stock,
                {
                    "name": name,
                    "code": code,
                    "mode": mode,
                    "basis": basis,
                    "perBands": per_bands,
                    "pbrBands": pbr_bands,
                    "prices": prices_text,
                    "actuals": actuals_text,
                    "forecast": forecast_text,
                },
            )
            db["stocks"][stock["code"]] = stock
            db["active"] = stock["code"]
            save_db(base, db)
            st.success("Oracle 서버에 저장했습니다.")

    sync_stock(
        stock,
        {
            "name": name,
            "code": code,
            "mode": mode,
            "basis": basis,
            "perBands": per_bands,
            "pbrBands": pbr_bands,
            "prices": prices_text,
            "actuals": actuals_text,
            "forecast": forecast_text,
        },
    )

    prices = parse_rows(prices_text, "price")
    actuals = parse_rows(actuals_text, "actual")
    forecast = parse_rows(forecast_text, "actual")
    bands = parse_bands(per_bands if mode == "PER" else pbr_bands)

    c1, c2, c3, c4 = st.columns(4)
    last_price = prices.iloc[-1] if not prices.empty else None
    last_actual = latest_actual(actuals, last_price["date"]) if last_price is not None else None
    metric_basis_df = (
        valuation_metric(actuals, forecast, pd.Series([last_price["date"]]), mode, basis)
        if last_price is not None
        else pd.DataFrame(columns=["date", "metric"])
    )
    last_metric = float(metric_basis_df.iloc[-1]["metric"]) if not metric_basis_df.empty else None
    mid = bands[len(bands) // 2] if bands else 1
    mid_value = last_metric * mid if last_metric is not None else None
    gap = (last_price["price"] / mid_value - 1) * 100 if last_price is not None and mid_value else None

    c1.metric("최근 주가", f"{last_price['price']:,.0f}" if last_price is not None else "-", str(last_price["date"].date()) if last_price is not None else "-")
    if last_actual is not None:
        metric_name = "BPS" if mode == "PBR" else "EPS"
        metric_value = last_metric if last_metric is not None else (last_actual["bps"] if mode == "PBR" else last_actual["eps"])
        c2.metric("적용 실적", f"{basis} {metric_name} {metric_value:,.0f}")
    else:
        c2.metric("적용 실적", "-")
    c3.metric("중앙 밴드 대비", f"{gap:+.1f}%" if gap is not None else "-", f"{mid:g}x 기준")
    c4.metric("미래 입력치", f"{len(forecast)}개")

    date_sources = []
    if not prices.empty and "date" in prices.columns:
        date_sources.append(prices["date"])
    if not forecast.empty and "date" in forecast.columns:
        date_sources.append(forecast["date"])
    if date_sources:
        available_dates = pd.concat(date_sources).dropna()
        min_chart_date = available_dates.min().date()
        max_chart_date = available_dates.max().date()
    else:
        min_chart_date = date(1990, 1, 1)
        max_chart_date = date.today()

    range_cols = st.columns([1, 1, 4])
    start_date = range_cols[0].date_input(
        "시작일",
        value=None,
        min_value=min_chart_date,
        max_value=max_chart_date,
    )
    end_date = range_cols[1].date_input(
        "종료일",
        value=None,
        min_value=min_chart_date,
        max_value=max_chart_date,
    )

    fig, future_all = build_chart(prices, actuals, forecast, mode, basis, bands, start_date, end_date)
    st.plotly_chart(fig, use_container_width=True)

    right_a, right_b = st.columns([1, 1])
    with right_a:
        st.subheader("미래 이론 주가")
        if not future_all.empty and bands:
            low, high = bands[0], bands[-1]
            rows = []
            for _, row in future_all.iterrows():
                rows.append(
                    {
                        "날짜": row["date"].date().isoformat(),
                        "하단": band_value(row, low, mode),
                        "중앙": band_value(row, mid, mode),
                        "상단": band_value(row, high, mode),
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("미래 실적을 입력하세요.")

    with right_b:
        st.subheader("과거 실적")
        if not actuals.empty:
            table = actuals.copy()
            table["date"] = table["date"].dt.date.astype(str)
            st.dataframe(table.rename(columns={"date": "날짜", "eps": "EPS", "bps": "BPS"}), hide_index=True, use_container_width=True)
        else:
            st.info("과거 실적을 입력하세요.")


if __name__ == "__main__":
    main()
