# テスト群ごとの所要時間（CI 高速化 第B段・第1段「計測」）

`test_runner.sigil` は、群が終わるたびに `timing <群>` の行と、その群の秒数の行を標準出力へ書く（最後に `timing total`）。
時計は `clock.unix_seconds` だけなので 1 秒単位で、1 秒未満は 0 になる。合否の判定（最後の行が `0`）は変わらない。
抜き出すには `grep -A1 '^timing' | grep -v '^--' | paste - -`。

## 実測（クラウドコンテナ、Linux x86_64、Xeon 2.10GHz・4 コア、他の負荷なし、main `7c45f75` + 計測）

| 群 | SIR 評価器（秒） | ネイティブ（秒） |
|---|---:|---:|
| limits | 0 | 0 |
| types | 0 | 0 |
| format | 1 | 1 |
| fixture | 3 | 2 |
| wal | 2 | 1 |
| btree | 21 | 12 |
| relation | 47 | 24 |
| carryover | 4 | 2 |
| backup | 16 | 9 |
| compat | 21 | 11 |
| acid | 48 | 27 |
| crash | 208 | 117 |
| fuzz | 171 | 94 |
| sql | 5 | 3 |
| **合計** | **547** | **303** |

CI の実測（GitHub Actions のランナー）は、この PR の CI ログの `timing` 行を見る。値はランナーの性能で変わるので、gate の選定は比率で行う。

## gate に入れる群の選定根拠（次の段の案）

- 評価器の 547 秒のうち、crash（208）・fuzz（171）・acid（48）の 3 群が 427 秒（78%）を占める。残りの 11 群は合計 120 秒（22%）。
- 3 群はいずれも長い故障注入・乱数操作列・性質テストで、PR の各変更を守る基本の回帰は残りの群で足りる。3 群は main への push の全量で必ず走らせる。
- gate の案: limits・types・format・fixture・wal・btree・relation・carryover・backup・compat・sql（11 群、評価器で約 2 分）。
- 新しい群は、gate の一覧に足すまで全量側だけで走る（外れる側に倒す）。
- 境界の判断: relation（47）と compat（21）は中程度。gate に入れても評価器で約 2 分に収まるので入れる案とした。時間が足りなければ relation を外す。
