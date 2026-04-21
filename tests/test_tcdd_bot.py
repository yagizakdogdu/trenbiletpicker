import unittest

from tcdd_bot import Search, build_payload, parse_availability


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


if __name__ == "__main__":
    unittest.main()
