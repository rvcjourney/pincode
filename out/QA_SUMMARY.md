# CK PIN code master - QA summary

| Metric | Value |
|---|---|
| Distinct PIN codes (all) | 20,463 |
| Active PIN codes (have a live post office) | 19,586 |
| Deliverable PIN codes | 19,563 |
| Retired PIN codes (boundary-only / no live office) | 877 |
| Post offices (current) | 165,595 |
| Post offices (closed, retained for audit) | 0 |
| PIN codes with polygon | 19,928 |
| PIN codes with centroid | 20,444 |
| PIN codes spanning >1 district | 1,256 |
| PIN codes spanning >1 state | 52 |
| States + UTs | 36 |
| Districts | 754 |
| change_log rows (all time) | 0 |

## Integrity checks

| Check | Count | Expect |
|---|---|---|
| Retired PINs still marked deliverable | 0 | 0 |
| PINs whose n_offices disagrees with post_office | 0 | 0 |
| Active PINs with no centroid (radius lookup fails) | 19 | investigate |

## Post offices by type

| Type | Count |
|---|---|
| BO | 140,240 |
| (unknown) | 24,544 |
| HO | 811 |

## Top 15 states by PIN code count

| State / UT | PIN codes | Post offices |
|---|---|---|
| Tamil Nadu | 2,041 | 11,733 |
| Uttar Pradesh | 1,666 | 17,956 |
| Maharashtra | 1,600 | 13,760 |
| Kerala | 1,428 | 5,052 |
| Karnataka | 1,359 | 9,658 |
| Andhra Pradesh | 1,248 | 10,681 |
| West Bengal | 1,131 | 8,786 |
| Rajasthan | 1,017 | 11,032 |
| Gujarat | 1,007 | 8,841 |
| Odisha | 943 | 8,913 |
| Bihar | 883 | 9,309 |
| Madhya Pradesh | 778 | 10,271 |
| Telangana | 683 | 6,250 |
| Assam | 575 | 4,063 |
| Punjab | 531 | 3,796 |

## Worst multi-district PIN codes (address-matching traps)

| PIN | Primary district | Districts | States | Offices |
|---|---|---|---|---|
| 795145 | Imphal East | 4 | 1 | 52 |
| 791001 | West Siang | 4 | 1 | 49 |
| 853204 | Bhagalpur | 4 | 1 | 34 |
| 791102 | East Siang | 4 | 1 | 29 |
| 360490 | Rajkot | 4 | 1 | 22 |
| 192124 | Anantnag | 4 | 1 | 20 |
| 795146 | Imphal West | 4 | 1 | 16 |
| 193501 | Bandipora | 4 | 1 | 13 |
| 533464 | Kakinada | 4 | 2 | 13 |
| 612201 | Mayiladuthurai | 4 | 1 | 6 |
| 790102 | East Kameng | 3 | 1 | 98 |
| 793119 | West Jaintia Hills | 3 | 1 | 75 |
| 804451 | Patna | 3 | 1 | 36 |
| 835227 | Khunti | 3 | 1 | 35 |
| 484001 | Shahdol | 3 | 1 | 32 |

## Provenance

| Source | Dataset | Authoritative | Rows |
|---|---|---|---|
| `datagov_directory` | [All India Pincode Directory till last month](https://www.data.gov.in/resource/all-india-pincode-directory-till-last-month) | yes | 328,362 |
| `lgd_local_bodies` | [Local Government Directory (LGD) - Local Bodies with PIN Codes](https://www.data.gov.in/) | yes | - |
| `lgd_villages` | [Local Government Directory (LGD) - Villages with PIN Codes](https://www.data.gov.in/) | yes | - |
| `bulk_snapshot` | [All India Pincode Directory (bulk CSV snapshot)](https://github.com/saravanakumargn/All-India-Pincode-Directory) | NO | - |
| `pincode_boundary` | [India PIN code boundary polygons + area](https://github.com/er-data-storage/postal-code-data) | NO | - |
