import base64
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from tcdd_bot import (
    Search,
    build_payload,
    expired_authorization_message,
    parse_availability,
)


def fake_jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class TcddBotTests(unittest.TestCase):
    def test_build_payload(self):
        search = Search(
            kalkis="Ankara Gar, Ankara",
            varis="İstanbul(Söğütlüçeşme)",
            tarih="2026-05-10",
        )

        self.assertEqual(
            build_payload(search),
            {
                "searchRoutes": [
                    {
                        "departureStation": "Ankara Gar, Ankara",
                        "arrivalStation": "İstanbul(Söğütlüçeşme)",
                        "departureDate": "2026-05-10 00:00:00",
                    }
                ],
                "passengerTypeCounts": [{"id": 0, "count": 1}],
                "searchReservation": False,
            },
        )

    def test_build_payload_with_station_ids_uses_current_api_shape(self):
        search = Search(
            kalkis="Ankara Gar, Ankara",
            varis="İstanbul(Söğütlüçeşme)",
            tarih="2026-04-22",
            kalkis_id=98,
            kalkis_api_adi="ANKARA GAR",
            varis_id=1325,
            varis_api_adi="İSTANBUL(SÖĞÜTLÜÇEŞME)",
        )

        self.assertEqual(
            build_payload(search),
            {
                "searchRoutes": [
                    {
                        "departureStationId": 98,
                        "departureStationName": "ANKARA GAR",
                        "arrivalStationId": 1325,
                        "arrivalStationName": "İSTANBUL(SÖĞÜTLÜÇEŞME)",
                        "departureDate": "21-04-2026 21:00:00",
                    }
                ],
                "passengerTypeCounts": [{"id": 0, "count": 1}],
                "searchReservation": False,
                "searchType": "DOMESTIC",
                "blTrainTypes": ["TURISTIK_TREN"],
            },
        )

    def test_parse_availability_filters_time_window(self):
        search = Search(
            kalkis="Ankara Gar, Ankara",
            varis="İstanbul(Söğütlüçeşme)",
            tarih="2026-05-10",
            min_saat="08:00",
            max_saat="14:00",
        )
        data = {
            "trainLegs": [
                {
                    "trainAvailabilities": [
                        {
                            "trains": [
                                {
                                    "commercialName": "YHT 1",
                                    "segments": [
                                        {"departureTime": "2026-05-10T09:05:00"}
                                    ],
                                    "cabinClassAvailabilities": [
                                        {
                                            "availabilityCount": 4,
                                            "cabinClass": {"name": "Ekonomi"},
                                        }
                                    ],
                                },
                                {
                                    "commercialName": "YHT 2",
                                    "segments": [
                                        {"departureTime": "2026-05-10T16:00:00"}
                                    ],
                                    "cabinClassAvailabilities": [
                                        {
                                            "availabilityCount": 8,
                                            "cabinClass": {"name": "Ekonomi"},
                                        }
                                    ],
                                },
                            ]
                        }
                    ]
                }
            ]
        }

        self.assertEqual(
            parse_availability(data, search),
            [
                {
                    "tren": "YHT 1",
                    "kalkis_saat": "2026-05-10T09:05:00",
                    "sinif": "Ekonomi",
                    "bos_koltuk": 4,
                }
            ],
        )

    def test_parse_availability_keeps_only_economy_by_default(self):
        search = Search(
            kalkis="Ankara Gar, Ankara",
            varis="İstanbul(Söğütlüçeşme)",
            tarih="2026-05-10",
            min_saat="08:00",
            max_saat="14:00",
        )
        data = {
            "trainLegs": [
                {
                    "trainAvailabilities": [
                        {
                            "trains": [
                                {
                                    "commercialName": "YHT 1",
                                    "segments": [
                                        {"departureTime": "2026-05-10T09:05:00"}
                                    ],
                                    "availableFareInfo": [
                                        {
                                            "fareFamily": {"name": "STANDART"},
                                            "cabinClasses": [
                                                {
                                                    "availabilityCount": 3,
                                                    "cabinClass": {"name": "BUSİNESS"},
                                                },
                                                {
                                                    "availabilityCount": 5,
                                                    "cabinClass": {"name": "EKONOMİ"},
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        self.assertEqual(
            parse_availability(data, search),
            [
                {
                    "tren": "YHT 1",
                    "kalkis_saat": "2026-05-10T09:05:00",
                    "sinif": "EKONOMİ",
                    "bos_koltuk": 5,
                }
            ],
        )

    def test_expired_authorization_message_detects_expired_jwt(self):
        token = fake_jwt({"exp": 1000})
        with mock.patch.dict("os.environ", {"TCDD_AUTHORIZATION": token}):
            message = expired_authorization_message(
                now=datetime.fromtimestamp(2000, timezone.utc)
            )

        self.assertIsNotNone(message)
        self.assertIn("süresi dolmuş", message)

    def test_expired_authorization_message_allows_current_jwt(self):
        token = fake_jwt({"exp": 3000})
        with mock.patch.dict("os.environ", {"TCDD_AUTHORIZATION": token}):
            message = expired_authorization_message(
                now=datetime.fromtimestamp(2000, timezone.utc)
            )

        self.assertIsNone(message)


if __name__ == "__main__":
    unittest.main()
