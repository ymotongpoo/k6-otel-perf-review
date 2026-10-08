# k6-otel-perf-review

同時実行数を制限したGoのHTTP APIに、k6で負荷をかけ、OpenTelemetryとGrafanaのスタックで遅い原因を調べるデモです。
Zenn本『その修正で本当に速くなったのか？k6とOpenTelemetryで確かめる性能改善』（CloudNative Days Winter 2026の登壇の解説資料、2026年11月18日公開予定）の数値は、この構成で測りました。

## 構成

商品の情報を返す`catalog-api`が、在庫を返す`inventory`をHTTPで呼びます。
テレメトリーはGrafana Alloyが受け取り、メトリクスをMimir、トレースをTempo、ログをLokiに振り分けます。

| サービス | 中身 |
| --- | --- |
| `api` | `catalog-api`。`GET /items/{id}`を受けると5ms処理し、`inventory`を呼ぶ。同時に呼ぶのは`API_CONCURRENCY`件まで（デフォルト4）。ホストの18080番 |
| `backend` | `inventory`。同時に処理するのは`BACKEND_CAPACITY`件まで（デフォルト6）、1件`BACKEND_SERVICE_MS`±`BACKEND_JITTER_MS`ms（デフォルト50±10）。待つ件数が`BACKEND_QUEUE`件（デフォルト8）を超えると503を返す |
| `k6` | grafana/k6 2.3.0。`docker compose run`で起動する（`tools`プロファイル） |
| `alloy` | grafana/alloy v1.20.1 |
| `mimir`、`tempo`、`loki` | grafana/mimir 3.2.2、grafana/tempo 3.1.0、grafana/loki 3.7.8。単一のプロセスで動かし、ローカルのファイルシステムに保存する |
| `grafana` | grafana/grafana 13.2.3。ホストの13000番。匿名でログインできる |

処理時間は`time.Sleep`で作っているので、CPUの性能に結果がほとんど左右されません。
コンテナイメージは`compose.yaml`と`Dockerfile`でダイジェストまで固定しています。

`INSTRUMENT_WAIT=true`にすると、`catalog-api`が`inventory`を呼ぶための空きを待つ区間に、スパン`wait for backend slot`とメトリクスを足します。
`TELEMETRY_DISABLED=true`にすると、2つのサービスともOpenTelemetryのSDKを初期化しません。

## 動かし方

DockerとDocker Composeが必要です。

```sh
docker compose up -d --build
curl http://localhost:18080/items/1
```

試験は`run.sh`で流します。
第1引数が試験の名前（k6の`testid`タグになる）で、続く`KEY=VALUE`がアプリケーションとk6の設定です。

```sh
# 開発担当の試験: 閉じたモデル、10VU、60秒（修正前の上限4）
./run.sh dev-l4 API_CONCURRENCY=4 MODEL=closed VUS=10 DURATION=60s

# 運用担当の試験: 開いたモデル、毎秒120件、60秒（修正後の上限16）
./run.sh ops-l16 API_CONCURRENCY=16 MODEL=open RATE=120 DURATION=60s CHECK_DROPPED=true
```

`API_*`、`BACKEND_*`、`INSTRUMENT_WAIT`、`TELEMETRY_DISABLED`、`K6_CPUS`を変えると、`run.sh`は`api`と`backend`を作り直します。
`K6_OUT=none`にすると、k6のOpenTelemetry出力を外します。

試験ごとに、k6の要約、終了コード、試験中のコンテナごとのCPU使用率、実際に動いていたコンテナの設定が、`results/runs.jsonl`に1行ずつ追記されます。
要約の`vus_max`は初期化したVUの数で、反復を実行中のVUの数は`vus`に別に残します。
`reqs.rate`は、完了したリクエストの数を、終了時の完了待ちを含む実行時間で割った値です。

## スクリプトと本の章

| ファイル | 内容 | 本の章 |
| --- | --- | --- |
| `k6/test.js` | 1回の反復で1件のリクエストを送る試験。`MODEL`で`constant-vus`と`constant-arrival-rate`を切り替える。`CHECK_DROPPED=true`で`dropped_iterations`の閾値を足す | 2章、3章、4章 |
| `run.sh` | 試験を流し、結果を`results/runs.jsonl`に残す | 全章 |
| `analyze_traces.py` | 試験の時間範囲のトレースをTempoから取り、スパンごとの時間を集計する。`python3 analyze_traces.py --same-set <試験の名前> 300`。`--same-set`を付けると、全部の行を両サービスのスパンがそろったトレース（`inventory`が受け付けたリクエスト）だけで集計する | 6章、7章、8章 |
| `replay.py` | `results/runs.jsonl`の1行を使って試験をやり直し、判定を比べる。`python3 replay.py <記録した試験の名前> <新しい試験の名前>`。負荷の条件は`args`から、環境変数とCPUの割り当ては`config`から戻す。コードは戻さないので、スクリプトのハッシュ、イメージ、CPUの割り当てが記録と違えば警告する | 9章 |
| `config/config.alloy` | Alloyの設定 | 5章 |
| `config/grafana/provisioning/datasources/datasources.yaml` | Grafanaのデータソースと、トレースからログへ移る設定 | 5章 |

Tempoは取り込みに時間がかかるので、`analyze_traces.py`は試験が終わってから1分ほど待って実行してください。
取り込みの途中で検索すると、返るトレースが偏ります。
取得したトレースが試験の反復の回数（または指定した件数）に届かないときは、`analyze_traces.py`が警告を出します。

## 手元で動かすときの注意

本の数値は、4コア、メモリ15GBのLinuxホストで、k6と被試験系を同じホストに載せて測りました。
`api`、`backend`、`k6`にはそれぞれ1コアを割り当てています（`API_CPUS`、`BACKEND_CPUS`、`K6_CPUS`で変えられます）。
ホストの性能や同居するプロセスによって、数値は本と少し変わります。
