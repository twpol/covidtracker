import json
import socket
import logging
import requests
import pandas as pd
import xarray as xr
import dns.resolver
from datetime import date
from urllib3.util import connection
import coviddata.uk
import coviddata.uk.scotland
import coviddata.uk.wales
import coviddata.world

from graphs import (
    uk_cases_graph,
    england_deaths,
    regional_cases,
    regional_deaths,
    triage_graph,
    la_rate_plot,
    hospital_admissions_graph,
    uk_test_positivity,
    uk_test_capacity,
    age_heatmap,
    risky_venues,
    app_keys,
)
from template import render_template
from map import map_data
from score import calculate_score
from corrections import correct_scottish_data, cases_by_nhs_region
from normalise import normalise_population
from nhs_app import NHSAppData

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("urllib3").setLevel(logging.INFO)
log = logging.getLogger(__name__)

log.info("Generating pages...")


def monkeypatch_connection(api_ip):
    from urllib3.util import connection

    _orig_create_connection = connection.create_connection

    def patched_create_connection(address, *args, **kwargs):
        host, port = address
        if host == "api.coronavirus.data.gov.uk":
            hostname = api_ip
        else:
            hostname = socket.gethostbyname(host)
        return _orig_create_connection((hostname, port), *args, **kwargs)

    connection.create_connection = patched_create_connection


#monkeypatch_connection(api_ip)


la_region = pd.read_csv(
    "https://raw.githubusercontent.com/russss/local_authority_nhs_region"
    "/master/local_authority_nhs_region.csv",
    index_col=["la_gss"],
)


def online_triage_by_nhs_region():
    triage_online = coviddata.uk.triage_nhs_online()
    triage = (
        triage_online.sum(["age_band", "sex"])
        .assign_coords(
            {"ccg": [ccg_lookup["NHSER20NM"][i.item()] for i in triage_online["ccg"]]}
        )
        .rename(ccg="region")
        .groupby("region")
        .sum()
    )

    triage["count_rolling_7"] = (
        triage["count"].fillna(0).rolling(date=7, center=True).mean().dropna("date")
    )
    return triage


def pathways_triage_by_nhs_region():
    triage_pathways = coviddata.uk.triage_nhs_pathways()
    triage_pathways = triage_pathways.where(
        triage_pathways.ccg.str.startswith("E"), drop=True
    )
    triage = (
        triage_pathways.sum(["age_band", "sex", "site_type"])
        .assign_coords(
            {"ccg": [ccg_lookup["NHSER20NM"][i.item()] for i in triage_pathways["ccg"]]}
        )
        .groupby("ccg")
        .sum()
        .rename(ccg="region")
    )

    triage["count_rolling_7"] = (
        triage["count"].fillna(0).rolling(date=7, center=True).mean().dropna("date")
    )
    return triage


ccg_lookup = (
    pd.read_csv("./data/ccg_region.csv").drop_duplicates("CCG20CD").set_index("CCG20CD")
)

populations = pd.read_csv("./data/region_populations.csv", thousands=",")
populations = populations[populations["Code"].str.len() == 9]
populations = (
    populations.rename(columns={"Code": "gss_code"})
    .set_index("gss_code")["All ages"]
    .to_xarray()
)

scot_populations = pd.read_csv("./data/scot_populations.csv", thousands=",").set_index(
    "gss code"
)

provisional_days = 7

uk_cases = coviddata.uk.cases_phe("countries")

uk_cases["cases_rolling"] = (
    uk_cases["cases"][:-provisional_days]
    .diff("date")
    .rolling(date=7, center=True)
    .mean()
    .dropna("date")
)

uk_cases["cases_rolling_provisional"] = (
    uk_cases["cases"].diff("date").rolling(date=7, center=True).mean().dropna("date")
)

eng_by_gss = coviddata.uk.cases_phe("ltlas", key="gss_code")
eng_by_gss["cases_norm"] = eng_by_gss["cases"] / populations

scot_data = correct_scottish_data(coviddata.uk.scotland.cases("gss_code"))


nhs_deaths = coviddata.uk.deaths_nhs()
nhs_deaths["deaths_rolling"] = (
    nhs_deaths["deaths"][:-provisional_days]
    .rolling(date=7, center=True)
    .mean()
    .dropna("date")
)
nhs_deaths["deaths_rolling_provisional"] = (
    nhs_deaths["deaths"].fillna(0).rolling(date=7, center=True).mean().dropna("date")
)

nhs_region_cases = cases_by_nhs_region(eng_by_gss, la_region)

nhs_region_cases["cases_rolling"] = (
    nhs_region_cases["cases"][:, :-provisional_days]
    .diff("date")
    .rolling(date=7, center=True)
    .mean()
    .dropna("date")
)

nhs_region_cases["cases_rolling_provisional"] = (
    nhs_region_cases["cases"]
    .diff("date")
    .rolling(date=7, center=True)
    .mean()
    .dropna("date")
)

hospital_admissions = coviddata.uk.hospitalisations_phe()
hospital_admissions["admissions_rolling"] = (
    hospital_admissions["admissions"]
    .diff("date")
    .rolling(date=7, center=True)
    .mean()
    .dropna("date")
)

excess_deaths = pd.read_csv(
    "./data/excess_deaths.csv", index_col="date", parse_dates=["date"], dayfirst=True
)
triage_online = online_triage_by_nhs_region()
triage_pathways = pathways_triage_by_nhs_region()

# phe_deaths = coviddata.uk.deaths_phe()

age_rate = coviddata.uk.case_rate_by_age()

render_template(
    "index.html",
    graphs={
        "confirmed_cases": uk_cases_graph(uk_cases),
        #        "deaths": england_deaths(phe_deaths, excess_deaths, uk_cases),
        "regional_cases": regional_cases(nhs_region_cases),
        "regional_deaths": regional_deaths(nhs_deaths),
        "triage_online": triage_graph(triage_online, "Online triage"),
        "triage_pathways": triage_graph(triage_pathways, "Phone triage"),
        "hospital_admissions": hospital_admissions_graph(hospital_admissions),
        "age_heatmap": age_heatmap(age_rate),
    },
    scores=calculate_score(
        nhs_deaths,
        nhs_region_cases,
        triage_online,
        triage_pathways,
        hospital_admissions,
    ),
    sources=[
        (
            "Public Health England",
            "Coronavirus (COVID-19) in the UK",
            "https://coronavirus.data.gov.uk",
            uk_cases.attrs["date"],
        ),
        (
            "Public Health England",
            "National COVID-19 surveillance report",
            "https://www.gov.uk/government/publications/national-covid-19-surveillance-reports",
            f"Week {age_rate.index.max() + 1}",
        ),
        #        (
        #            "ONS",
        #            "Deaths registered weekly in England and Wales, provisional",
        #            "https://www.ons.gov.uk/peoplepopulationandcommunity/birthsdeathsandmarriages"
        #            "/deaths/datasets/weeklyprovisionalfiguresondeathsregisteredinenglandandwales",
        #            date(2020, 7, 28),
        #        ),
        (
            "NHS",
            "Potential COVID-19 symptoms reported through NHS Pathways and 111 online",
            "https://digital.nhs.uk/data-and-information/publications/statistical"
            "/mi-potential-covid-19-symptoms-reported-through-nhs-pathways-and-111-online",
            pd.Timestamp(triage_online["date"].max().item(0)).date(),
        ),
        (
            scot_data.attrs["source"],
            "Coronavirus - COVID-19 - Management Information",
            scot_data.attrs["source_url"],
            scot_data.attrs["date"],
        ),
    ],
)

testing = coviddata.uk.tests_phe()

cases_by_publish_date = coviddata.uk.cases_phe(by="overview", basis="report")
uk_cases_pub_date = cases_by_publish_date.sel(location="United Kingdom").diff("date")[
    "cases"
]
uk_cases_pub_date = uk_cases_pub_date.where(uk_cases_pub_date > 0)
positivity = uk_cases_pub_date / (
    testing["newPillarOneTestsByPublishDate"]
    + testing["newPillarTwoTestsByPublishDate"]
)


render_template(
    "testing.html",
    graphs={
        "positivity": uk_test_positivity(positivity),
        "test_capacity": uk_test_capacity(testing),
    },
    sources=[
        (
            testing.attrs["source"],
            "Coronavirus (COVID-19) in the UK",
            testing.attrs["source_url"],
            testing.attrs["date"],
        )
    ],
)


render_template(
    "map.html",
    data=json.dumps(map_data(eng_by_gss, provisional_days)),
    provisional_days=provisional_days,
    sources=[
        (
            "Public Health England",
            "Coronavirus (COVID-19) in the UK",
            "https://coronavirus.data.gov.uk",
            uk_cases.attrs["date"],
        ),
    ],
)


def slugify(string):
    return string.lower().replace(" ", "-")


heat_plots = {}

for region in la_region["nhs_name"].unique():
    las = la_region[la_region["nhs_name"] == region]
    region_data = eng_by_gss.where(
        eng_by_gss["gss_code"].isin(list(las.index)), drop=True
    )
    names = la_region["la_name"].sort_values(ascending=False)
    heat_plots[slugify(region)] = la_rate_plot(region_data, names, region)


render_template("areas.html", graphs=heat_plots)

app_data = NHSAppData()
exposures = app_data.exposures()
render_template(
    "app.html",
    graphs={
        "risky_venues": risky_venues(app_data.risky_venues()),
        "app_keys": app_keys(exposures),
        "app_keys_risk": app_keys(exposures, by="interval")
    },
    sources=[
        (
            "Russ Garrett",
            "NHS COVID-19 App Data",
            "https://github.com/russss/nhs-covid19-app-data",
            date.today(),
        )
    ],
)
