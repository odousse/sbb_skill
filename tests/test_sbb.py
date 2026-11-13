import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SBB_SCRIPT = REPO_ROOT / "skills" / "sbb-trip-planner" / "scripts" / "sbb.py"

spec = importlib.util.spec_from_file_location("sbb", SBB_SCRIPT)
assert spec and spec.loader
sbb = importlib.util.module_from_spec(spec)
sys.modules["sbb"] = sbb
spec.loader.exec_module(sbb)


SAMPLE_SBB_RECORDS = [
    {
        "title": "Limited service: Lausanne - Fribourg/Freiburg",
        "link": "https://www.sbb.ch/en/travel-information/rail-traffic-information/operations-disruptions.expandedheader.html/1239538",
        "description": "13.11.2026 08:49 - 16:49Limited train service between Lausanne and Fribourg/Freiburg.This is due to a damage to the tracks.",
        "published": "2026-11-13T07:53:32+00:00",
        "cause": None,
        "startdatetime": "2026-11-13T08:49:00+00:00",
        "enddatetime": "2026-11-13T16:49:00+00:00",
        "dailyvaliditybegin": "00:00:00",
        "dailyvalidityend": "23:59:00",
        "type": "2",
        "author": "SBB",
        "description_html": (
            "13.11.2026 08:49 - 16:49<br /><br />"
            "Limited train service between Lausanne and Fribourg/Freiburg.<br />"
            "This is due to a damage to the tracks.<br />"
            "Lines IC1, IR15 and S40 are affected.<br />"
            "Der Zug verkehrt mit Verspätung."
        ),
    }
]

SAMPLE_SIRI_SX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<siri:Siri xmlns:siri="http://www.siri.org.uk/siri">
  <siri:ServiceDelivery>
    <siri:SituationExchangeDelivery>
      <siri:Situations>
        <siri:PtSituationElement>
          <siri:SituationNumber>CH-999</siri:SituationNumber>
          <siri:Progress>open</siri:Progress>
          <siri:Planned>false</siri:Planned>
          <siri:Severity>severe</siri:Severity>
          <siri:ValidityPeriod>
            <siri:StartTime>2026-11-13T10:00:00Z</siri:StartTime>
            <siri:EndTime>2026-11-13T18:00:00Z</siri:EndTime>
          </siri:ValidityPeriod>
          <siri:Summary xml:lang="de">Unterbruch Olten - Aarau</siri:Summary>
          <siri:Summary xml:lang="en">Service interrupted Olten - Aarau</siri:Summary>
          <siri:Description xml:lang="en">Overhead line damage between Olten and Aarau.</siri:Description>
          <siri:Affects>
            <siri:Networks>
              <siri:AffectedNetwork>
                <siri:AffectedLine>
                  <siri:PublishedLineName>IC 1</siri:PublishedLineName>
                </siri:AffectedLine>
              </siri:AffectedNetwork>
            </siri:Networks>
            <siri:StopPoints>
              <siri:AffectedStopPoint>
                <siri:StopPointName>Olten</siri:StopPointName>
              </siri:AffectedStopPoint>
            </siri:StopPoints>
          </siri:Affects>
        </siri:PtSituationElement>
        <siri:PtSituationElement>
          <siri:SituationNumber>CH-CLOSED</siri:SituationNumber>
          <siri:Progress>closed</siri:Progress>
          <siri:Summary xml:lang="en">Resolved alert</siri:Summary>
        </siri:PtSituationElement>
      </siri:Situations>
    </siri:SituationExchangeDelivery>
  </siri:ServiceDelivery>
</siri:Siri>
""".encode("utf-8")

SAMPLE_OPENDATA_CONNECTIONS = {
    "from": {"name": "Zürich HB"},
    "to": {"name": "Lausanne"},
    "connections": [
        {
            "from": {
                "station": {"name": "Zürich HB"},
                "departure": "2026-11-13T13:02:00+0100",
                "prognosis": {"departure": "2026-11-13T13:07:00+0100"},
            },
            "to": {
                "station": {"name": "Lausanne"},
                "arrival": "2026-11-13T15:10:00+0100",
                "prognosis": {"arrival": "2026-11-13T15:10:00+0100"},
            },
            "duration": "00d02:08:00",
            "transfers": 1,
            "sections": [
                {
                    "journey": {
                        "category": "IC",
                        "number": "8",
                        "operator": "SBB",
                        "to": "Brig",
                        "capacity1st": 1,
                        "capacity2nd": 3,
                        "passList": [
                            {"station": {"name": "Zürich HB"}},
                            {"station": {"name": "Bern"}},
                        ],
                    },
                    "departure": {
                        "station": {"name": "Zürich HB"},
                        "departure": "2026-11-13T13:02:00+0100",
                        "platform": "31",
                        "prognosis": {
                            "departure": "2026-11-13T13:07:00+0100",
                            "platform": "32",
                        },
                    },
                    "arrival": {
                        "station": {"name": "Bern"},
                        "arrival": "2026-11-13T13:58:00+0100",
                        "platform": "7",
                        "prognosis": {
                            "arrival": "2026-11-13T14:02:00+0100",
                            "platform": "7",
                        },
                    },
                },
                {
                    "journey": {
                        "category": "IR",
                        "number": "15",
                        "operator": "SBB",
                        "to": "Genève-Aéroport",
                        "passList": [
                            {"station": {"name": "Bern"}},
                            {"station": {"name": "Fribourg/Freiburg"}},
                            {"station": {"name": "Lausanne"}},
                        ],
                    },
                    "departure": {
                        "station": {"name": "Bern"},
                        "departure": "2026-11-13T14:04:00+0100",
                        "platform": "3",
                        "prognosis": {
                            "departure": "2026-11-13T14:04:00+0100",
                            "platform": "3",
                        },
                    },
                    "arrival": {
                        "station": {"name": "Lausanne"},
                        "arrival": "2026-11-13T15:10:00+0100",
                        "platform": "4",
                        "prognosis": {
                            "arrival": "2026-11-13T15:10:00+0100",
                            "platform": "4",
                        },
                    },
                },
            ],
        }
    ],
}

SAMPLE_OJP_LIR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri" version="2.0">
  <OJPResponse>
    <siri:ServiceDelivery>
      <OJPLocationInformationDelivery>
        <PlaceResult>
          <Place>
            <StopPlace>
              <StopPlaceRef>ch:1:sloid:7000</StopPlaceRef>
              <StopPlaceName><Text>Bern</Text></StopPlaceName>
            </StopPlace>
            <GeoPosition>
              <siri:Longitude>7.4391</siri:Longitude>
              <siri:Latitude>46.9488</siri:Latitude>
            </GeoPosition>
          </Place>
        </PlaceResult>
      </OJPLocationInformationDelivery>
    </siri:ServiceDelivery>
  </OJPResponse>
</OJP>
"""

SAMPLE_OJP_TRIP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OJP xmlns="http://www.vdv.de/ojp" xmlns:siri="http://www.siri.org.uk/siri" version="2.0">
  <OJPResponse>
    <siri:ServiceDelivery>
      <OJPTripDelivery>
        <TripResponseContext>
          <Situations>
            <PtSituationElement>
              <SituationNumber>SIT-42</SituationNumber>
              <Summary xml:lang="en">Track work near Fribourg</Summary>
            </PtSituationElement>
          </Situations>
        </TripResponseContext>
        <TripResult>
          <Id>1</Id>
          <Trip>
            <Duration>PT1H06M</Duration>
            <Transfers>0</Transfers>
            <Leg>
              <TimedLeg>
                <LegBoard>
                  <StopPointName><Text>Bern</Text></StopPointName>
                  <ServiceDeparture>
                    <TimetabledTime>2026-11-13T13:04:00Z</TimetabledTime>
                    <EstimatedTime>2026-11-13T13:07:00Z</EstimatedTime>
                  </ServiceDeparture>
                  <PlannedQuay><Text>3</Text></PlannedQuay>
                  <EstimatedQuay><Text>4</Text></EstimatedQuay>
                </LegBoard>
                <LegIntermediate>
                  <StopPointName><Text>Fribourg/Freiburg</Text></StopPointName>
                </LegIntermediate>
                <LegAlight>
                  <StopPointName><Text>Lausanne</Text></StopPointName>
                  <ServiceArrival>
                    <TimetabledTime>2026-11-13T14:10:00Z</TimetabledTime>
                    <EstimatedTime>2026-11-13T14:12:00Z</EstimatedTime>
                  </ServiceArrival>
                  <PlannedQuay><Text>5</Text></PlannedQuay>
                </LegAlight>
                <Service>
                  <PublishedServiceName><Text>IC 1</Text></PublishedServiceName>
                  <DestinationText><Text>Genève-Aéroport</Text></DestinationText>
                  <Cancelled>false</Cancelled>
                  <SituationFullRefs>
                    <SituationFullRef>
                      <siri:SituationNumber>SIT-42</siri:SituationNumber>
                    </SituationFullRef>
                  </SituationFullRefs>
                </Service>
              </TimedLeg>
            </Leg>
          </Trip>
        </TripResult>
      </OJPTripDelivery>
    </siri:ServiceDelivery>
  </OJPResponse>
</OJP>
""".encode("utf-8")


class SbbSkillTests(unittest.TestCase):
    def test_iso_and_user_datetime_parsing_py39(self) -> None:
        # Colon-less offset from transport.opendata.ch + UTC Z + fractional seconds
        dt_opendata = sbb.parse_iso_dt("2026-11-13T13:02:00+0100")
        self.assertIsNotNone(dt_opendata)
        self.assertEqual(sbb.fmt_time(dt_opendata), "13:02")

        dt_utc = sbb.parse_iso_dt("2026-11-13T12:02:00.795Z")
        self.assertIsNotNone(dt_utc)
        self.assertEqual(sbb.fmt_time(dt_utc), "13:02")  # CET = UTC+1 in Nov

        ref = datetime(2026, 11, 13, 10, 0, tzinfo=sbb.ZURICH_TZ)
        tmrw = sbb.parse_user_datetime("tomorrow 08:15", ref_now=ref)
        self.assertEqual(sbb.fmt_dt(tmrw), "2026-11-14 08:15")

    def test_duration_parsing(self) -> None:
        self.assertEqual(sbb.parse_duration_minutes("00d02:08:00"), 128)
        self.assertEqual(sbb.parse_duration_minutes("PT1H06M"), 66)
        self.assertEqual(sbb.fmt_duration_minutes(128), "2h 08m")
        self.assertEqual(sbb.fmt_duration_minutes(45), "45m")

    def test_sbb_disruption_parsing_and_zug_false_positive_guard(self) -> None:
        parsed = sbb.parse_sbb_disruption_records(SAMPLE_SBB_RECORDS)
        self.assertEqual(len(parsed), 1)
        alert = parsed[0]
        self.assertEqual(alert["id"], "1239538")
        self.assertIsNone(alert["daily_window"])
        self.assertIn("IC1", alert["affected_lines"])
        self.assertIn("IR15", alert["affected_lines"])
        # Ensure HTML <br /> produced clean newlines rather than glued sentences
        self.assertIn("\nLimited train service", alert["description"])

        ref = sbb.parse_iso_dt("2026-11-13T12:00:00+01:00")
        # Should match bilingual Fribourg
        fribourg_hits = sbb.filter_disruptions(parsed, stations=["Fribourg"], ref_time=ref)
        self.assertEqual(len(fribourg_hits), 1)

        # Should NOT match station "Zug" just because German text says "Der Zug verkehrt..."
        zug_hits = sbb.filter_disruptions(parsed, stations=["Zug"], ref_time=ref)
        self.assertEqual(len(zug_hits), 0)

    def test_siri_sx_parsing_and_closed_filtering(self) -> None:
        alerts = sbb.parse_siri_sx_xml(SAMPLE_SIRI_SX_XML, preferred_lang="en")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["id"], "CH-999")
        self.assertEqual(alerts[0]["title"], "Service interrupted Olten - Aarau")
        self.assertEqual(alerts[0]["affected_lines"], ["IC 1"])
        self.assertEqual(alerts[0]["affected_stops"], ["Olten"])

    def test_opendata_trip_delays_platform_change_and_transfer_warning(self) -> None:
        with mock.patch.object(sbb, "http_get_json", return_value=SAMPLE_OPENDATA_CONNECTIONS):
            res = sbb.opendata_plan_trip("Zürich HB", "Lausanne")
        self.assertEqual(len(res["connections"]), 1)
        conn = res["connections"][0]
        leg1, leg2 = conn["legs"]
        self.assertEqual(leg1["dep_delay_mins"], 5)
        self.assertTrue(leg1["dep_platform_changed"])
        self.assertEqual(leg1["dep_platform"], "32")
        self.assertEqual(leg1["dep_platform_sched"], "31")
        # IC 8 arrives at 14:02 (+4m), IR 15 leaves at 14:04 -> 2 min CRITICAL TRANSFER
        self.assertEqual(len(conn["transfer_warnings"]), 1)
        self.assertIn("CRITICAL TRANSFER at Bern", conn["transfer_warnings"][0])
        # Intermediate passList stop Fribourg/Freiburg captured for corridor disruption check
        self.assertIn("Fribourg/Freiburg", conn["corridor_stations"])

        md = sbb.format_trip_markdown(res)
        self.assertIn("Pl. 32 (changed from 31!)", md)
        self.assertIn("CRITICAL TRANSFER at Bern", md)

    def test_ojp20_trip_parsing_and_embedded_situations(self) -> None:
        import xml.etree.ElementTree as ET

        lir_el = ET.fromstring(SAMPLE_OJP_LIR_XML)
        trip_el = ET.fromstring(SAMPLE_OJP_TRIP_XML)
        with mock.patch.object(sbb, "_ojp_post", side_effect=[lir_el, lir_el, trip_el]):
            res = sbb.ojp_plan_trip("fake-key", "Bern", "Lausanne", lang="en")

        self.assertEqual(len(res["connections"]), 1)
        conn = res["connections"][0]
        self.assertEqual(conn["duration_mins"], 66)
        self.assertEqual(len(conn["situations"]), 1)
        self.assertIn("Track work near Fribourg", conn["situations"][0])
        leg = conn["legs"][0]
        self.assertEqual(leg["dep_delay_mins"], 3)
        self.assertTrue(leg["dep_platform_changed"])
        self.assertEqual(leg["dep_platform"], "4")
        self.assertIn("Fribourg/Freiburg", conn["corridor_stations"])

    def test_package_zip_bundles(self) -> None:
        import subprocess
        import zipfile

        pkg_script = REPO_ROOT / "package_zip.sh"
        subprocess.run(["bash", str(pkg_script)], check=True, cwd=str(REPO_ROOT))

        full_zip = REPO_ROOT / "dist" / "sbb-trip-planner.zip"
        gemini_zip = REPO_ROOT / "dist" / "sbb-trip-planner-gemini.zip"
        self.assertTrue(full_zip.is_file())
        self.assertTrue(gemini_zip.is_file())

        with zipfile.ZipFile(full_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("sbb-trip-planner/SKILL.md", names)
            self.assertIn("sbb-trip-planner/scripts/sbb.py", names)
            self.assertIn("sbb-trip-planner/references/api-details.md", names)
            self.assertTrue(all(not p.endswith(".DS_Store") for p in names))

        with zipfile.ZipFile(gemini_zip) as zf:
            names = set(zf.namelist())
            self.assertIn("sbb-trip-planner/SKILL.md", names)
            self.assertIn("sbb-trip-planner/references/api-details.md", names)
            self.assertNotIn("sbb-trip-planner/scripts/sbb.py", names)


if __name__ == "__main__":
    unittest.main()
