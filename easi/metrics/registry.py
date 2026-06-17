"""Registry of implemented EASI metric adapters (metricId -> callable).

As adapters land (Phases 2-3) they are added here. Metrics absent from this
registry render as 'pending' in the report and are excluded from the rollup
(graceful degradation), so the app is always runnable end-to-end.
"""
from __future__ import annotations

from . import biology, geomorphology, hydraulics, hydrology, physicochemistry

REGISTRY = {
    # Hydrology
    hydrology.IMPERVIOUS_ID: hydrology.impervious,
    hydrology.WETLANDS_ID: hydrology.wetlands,
    hydrology.FLOW_ALTERATION_ID: hydrology.flow_alteration,
    hydrology.REACH_INFLOW_ID: hydrology.reach_inflow,
    # Hydraulics
    hydraulics.LOW_FLOW_ID: hydraulics.low_flow_connectivity,
    hydraulics.HYPORHEIC_ID: hydraulics.hyporheic,
    hydraulics.ENTRENCHMENT_ID: hydraulics.floodplain_access,
    hydraulics.FLOODPLAIN_ENGAGEMENT_ID: hydraulics.floodplain_engagement,
    # Geomorphology
    geomorphology.SEDIMENT_ID: geomorphology.sediment_supply,
    geomorphology.SUBSTRATE_ID: geomorphology.substrate,
    geomorphology.BANK_EROSION_ID: geomorphology.bank_erosion,
    geomorphology.CHANNEL_EVOL_ID: geomorphology.channel_evolution,
    # Physicochemistry
    physicochemistry.IMPAIRMENT_ID: physicochemistry.impairment,
    physicochemistry.CPOM_ID: physicochemistry.detrital_cpom,
    physicochemistry.NUTRIENTS_ID: physicochemistry.nutrients,
    physicochemistry.TEMPERATURE_ID: physicochemistry.stream_temperature,
    # Biology
    biology.INVASIVES_ID: biology.invasives,
    biology.BARRIERS_ID: biology.barriers,
    biology.HABITAT_ID: biology.habitat_complexity,
    biology.BIOINTEGRITY_ID: biology.biological_integrity,
}

# StreamCat base metric names needed by the registered adapters (one batched call
# returns ws / cat / wsrp100 / catrp100 variants for each).
STREAMCAT_NAMES = [
    "pctimp2019", "pctwdwet2019", "pcthbwet2019",
    "pctcrop2019", "pcthay2019",
    "pctmxfst2019", "pctdecid2019", "pctconif2019",
    "kffact", "rddens", "damdens", "damnrmstor",
    "tmean8110",  # PRISM 1981-2010 mean-annual air-temp normal (climate surrogate for stream temp)
]
