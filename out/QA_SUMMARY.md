# CK PIN code master - QA summary

| Metric | Value |
|---|---|
| Distinct PIN codes (all) | 19,936 |
| Active PIN codes (have a live post office) | 19,100 |
| Deliverable PIN codes | 19,092 |
| Retired PIN codes (boundary-only / no live office) | 836 |
| Post offices (current) | 154,781 |
| Post offices (closed, retained for audit) | 0 |
| PIN codes with polygon | 19,928 |
| PIN codes with centroid | 19,928 |
| PIN codes spanning >1 district | 1,245 |
| PIN codes spanning >1 state | 34 |
| States + UTs | 36 |
| Districts | 632 |
| change_log rows (all time) | 0 |

## Integrity checks

| Check | Count | Expect |
|---|---|---|
| Retired PINs still marked deliverable | 0 | 0 |
| PINs whose n_offices disagrees with post_office | 0 | 0 |
| Active PINs with no centroid (radius lookup fails) | 8 | investigate |

## Post offices by type

| Type | Count |
|---|---|
| BO | 125,260 |
| SO | 24,769 |
| (unknown) | 3,942 |
| HO | 810 |

## Top 15 states by PIN code count

| State / UT | PIN codes | Post offices |
|---|---|---|
| Tamil Nadu | 2,020 | 11,870 |
| Uttar Pradesh | 1,627 | 17,669 |
| Maharashtra | 1,576 | 12,608 |
| Kerala | 1,407 | 5,057 |
| Karnataka | 1,327 | 9,681 |
| Andhra Pradesh | 1,203 | 10,334 |
| West Bengal | 1,174 | 8,724 |
| Gujarat | 1,016 | 8,937 |
| Rajasthan | 979 | 10,334 |
| Odisha | 907 | 8,166 |
| Bihar | 858 | 9,061 |
| Madhya Pradesh | 765 | 8,311 |
| Telangana | 659 | 5,808 |
| Assam | 570 | 4,010 |
| Punjab | 525 | 3,808 |

## Worst multi-district PIN codes (address-matching traps)

| PIN | Primary district | Districts | States | Offices |
|---|---|---|---|---|
| 853204 | Bhagalpur | 5 | 1 | 34 |
| 393130 | Bharuch | 4 | 1 | 25 |
| 360490 | Rajkot | 4 | 1 | 23 |
| 392150 | Bharuch | 4 | 1 | 21 |
| 184121 | Jammu | 4 | 1 | 18 |
| 192121 | Ananthnag | 4 | 1 | 8 |
| 835302 | Lohardaga | 3 | 1 | 42 |
| 811311 | Lakhisarai | 3 | 1 | 32 |
| 461111 | Hoshangabad | 3 | 1 | 29 |
| 484001 | Shahdol | 3 | 1 | 28 |
| 193401 | Baramulla | 3 | 1 | 24 |
| 813221 | Munger | 3 | 1 | 24 |
| 461228 | Harda | 3 | 1 | 23 |
| 811315 | Jamui | 3 | 1 | 22 |
| 193303 | Ananthnag | 3 | 1 | 21 |

## Provenance

| Source | Dataset | Authoritative | Rows |
|---|---|---|---|
| `datagov_directory` | [All India Pincode Directory till last month](https://www.data.gov.in/resource/all-india-pincode-directory-till-last-month) | yes | 756,600 |
| `lgd_local_bodies` | [Local Government Directory (LGD) - Local Bodies with PIN Codes](https://www.data.gov.in/) | yes | - |
| `lgd_villages` | [Local Government Directory (LGD) - Villages with PIN Codes](https://www.data.gov.in/) | yes | - |
| `bulk_snapshot` | [All India Pincode Directory (bulk CSV snapshot)](https://github.com/saravanakumargn/All-India-Pincode-Directory) | NO | 464,343 |
| `pincode_boundary` | [India PIN code boundary polygons + area](https://github.com/er-data-storage/postal-code-data) | NO | - |
