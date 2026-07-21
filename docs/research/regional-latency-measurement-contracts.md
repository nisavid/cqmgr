# Regional latency-measurement contracts

Google Cloud does not publish a universal, active probe endpoint or protocol
for measuring one client's latency to every Google Cloud region without first
deploying or selecting a regional target. The first Cloud Quotas manager
release therefore cannot safely derive a comparable client-to-region latency
ranking by pinging Google-owned hostnames.

Google does publish aggregate city-to-region latency in Performance Dashboard.
That signal can inform placement, but it is passive, population-level evidence,
not a measurement from the current client. Regional Google API endpoints are
also real regional network targets, but Google documents them as service and
data-residency endpoints rather than latency probes. Their service coverage,
network tier, request semantics, and quotas prevent treating response time as a
uniform regional-latency contract.

This note records the public contract as of 2026-07-21. All cited sources are
Google Cloud documentation.

## Evaluated provider surfaces

| Surface | Requires product-created regional resources | What it measures | Suitable as a universal client-to-region measurement |
| --- | --- | --- | --- |
| Performance Dashboard, Google Cloud performance view | No | Median RTT from passively sampled TCP traffic between internet geographies and VMs in Google Cloud regions, aggregated across Google Cloud. | No. It is an aggregate for a city, geographic region, or country, not the current client, and a region/geography pair appears only with sufficient traffic. |
| Performance Dashboard, project view / Cloud Monitoring `vm_flow` metrics | No new resource if qualifying traffic already exists | RTT sampled from a project's existing VM-to-VM or VM-to-internet TCP traffic. | No. It only covers regions with existing VMs and sufficient traffic, so it cannot compare undeployed candidate regions. |
| Public regional Google API endpoints | No product-created resource, but the service must support the region and API use can require project setup and authorization. | An API request to a service plane whose TLS session terminates in the named region. | No. Google defines no probe method, payload, sampling contract, latency statistic, or cross-service comparability. |
| Global Google API endpoints | No | A service request whose TLS session terminates near the client on Google's global frontend. | No. The endpoint does not measure the path to the named workload region. |
| Connectivity Tests live data plane analysis | Existing supported Google Cloud endpoints are required. | Reachability, packet delivery, and latency between configured endpoints for eligible paths. | No. It diagnoses selected existing endpoints; it is not an endpoint from an arbitrary client to every candidate region. |
| Cloud Network Insights Monitoring Points | Yes, or an agent must be installed on an existing host. | Active synthetic ICMP, UDP, or TCP measurements between configured monitoring points and targets. | No for a non-provisioning workflow. It requires deployed agents, control-plane configuration, and target-specific network access. |
| Public uptime checks | A check configuration and a public target are required. | HTTP, HTTPS, or TCP request latency from Google's fixed checker locations to the target. | No. The source is a Google checker rather than the current client, and the target must already exist. |

Performance Dashboard is designed to help plan deployments by comparing
median RTT between internet geographies and Google Cloud regions. The Google
Cloud view can be used even when the selected project has no deployment in the
candidate region, but an individual region/geography path remains conditional
on sufficient aggregate traffic. Google states that the values are not network
performance targets or SLA evidence. [Performance Dashboard
overview](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/concepts/overview)
[Planning across
geographies](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/concepts/use-cases-performance-across-geographies)

## Performance Dashboard contract

### Measurement and coverage

Google calculates latency from sampled TCP traffic. It measures the elapsed
time from a TCP sequence number to the corresponding acknowledgement, so the
value contains network RTT plus TCP-stack-related delay and can include
application delay. Dashboard values are medians. Project-specific data comes
from the selected project's traffic; Google Cloud performance data aggregates
traffic across Google Cloud. The internet measurements are passive samples,
not active probes sent to internet clients. [Metrics and
views](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/concepts/metrics-views)

The internet view supports city, geographic-region, and country aggregation,
Standard or Premium network-tier filtering, and time windows from one hour to
six weeks. A specific geography/region pair is available only when Google has
sufficient traffic for that pair. Events can take up to ten minutes to appear.
Consequently, absence is `unavailable`, not zero latency, and coverage cannot
be assumed to match every accelerator-catalog region. [View project-specific
latency](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/how-to/view-project-specific-latency)
[Metrics and
views](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/concepts/metrics-views)

The documented Cloud Monitoring exports do not provide a machine-readable
equivalent of every internet-geography aggregate shown in the Google Cloud
performance view:

- `networking.googleapis.com/all_gcp/vm_traffic/zone_pair_median_rtt` is the
  all-project **VM-to-VM** median RTT for a zone pair, not client-to-region
  latency.
- `networking.googleapis.com/vm_flow/external_rtt` is a distribution for TCP
  connections between an existing project VM and internet endpoints. It is
  bound to project traffic and `gce_instance` resources.

The metrics reference documents Cloud Monitoring access for these metrics but
does not specify a public API metric for the dashboard's all-Google-Cloud
city-to-region internet view. The console view must not be scraped or reverse
engineered as an undocumented API. [Performance Dashboard metrics
reference](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/how-to/viewing-perf-dash-metrics)

### Access, security, and rate limits

Performance Dashboard and its Monitoring data require
`monitoring.timeSeries.list`; Monitoring Viewer is the least-privileged
predefined role documented for dashboard access. The Monitoring REST method
uses OAuth and requires a project, organization, or folder resource name.
Credentials and project identifiers must remain in the existing authenticated
Google Cloud client boundary rather than in a latency record. [Performance
Dashboard overview](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/concepts/overview)
[Cloud Monitoring `timeSeries.list`](https://cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list)

Google does not publish a dashboard refresh allowance as a latency-sampling
contract. Monitoring query quotas are project quotas discoverable in the
project's quota dashboard, and Google says internal Monitoring endpoints are
not intended for high request rates. Any future Monitoring integration must
honor the live project quota, page tokens, backoff, and normal observation
freshness rather than poll it as an active probe. [Cloud Monitoring quotas and
limits](https://cloud.google.com/monitoring/quotas)

### Interpretation limits

The aggregate is useful only when its provenance stays attached:

- source geography and its granularity;
- destination region;
- observation time or requested interval;
- network tier selection;
- median statistic and milliseconds unit;
- provider-aggregate source kind; and
- availability or missing-data reason.

It must not be labeled "my latency," converted to a zone-specific value,
treated as a current route measurement, or compared with a differently sourced
number without showing the source difference. Google notes that aggregate and
project data can diverge because they incorporate different network paths, and
that sampling can overstate RTT when a later application response is matched as
the acknowledgement. [Metrics and
views](https://cloud.google.com/network-intelligence-center/docs/performance-dashboard/concepts/metrics-views)

## Why regional Google API endpoints are not probes

Public regional endpoints use hostnames of the form
`SERVICE.REGION.rep.googleapis.com`. Google states that processing and TLS
termination occur in the named region and that public internet traffic uses
Standard Tier networking. This makes the hostname region-bound, but does not
make it a latency-test service. [Google Cloud API endpoint
overview](https://cloud.google.com/docs/security/compliance/endpoints)
[Access public regional
endpoints](https://cloud.google.com/docs/security/compliance/access-public-regional-endpoints)

They lack the properties required for comparable measurements:

1. **Coverage is service-specific.** Only listed service/region combinations
   exist. The supported regional service endpoint table is not the accelerator
   catalog and can change independently of it. [Regional service
   endpoints](https://cloud.google.com/docs/security/compliance/regional-service-endpoints)
2. **Requests have service semantics.** Google documents these endpoints for
   API operations. Services can require API enablement, OAuth, resource names,
   request bodies, and IAM permissions. There is no common read-only probe path
   or response status.
3. **Quotas are not uniform.** A request consumes the target service's request
   path and is governed by that service's quotas and abuse protections. There
   is no provider-defined cross-service probe rate.
4. **Topology differs from likely workloads.** Public regional API traffic uses
   Standard Tier and terminates in a Google service plane. A VM, TPU, GKE
   workload, load balancer, private path, or Premium Tier path can follow a
   different route and termination topology.
5. **Timing is composite.** DNS, connection reuse, TCP, TLS, authentication,
   service processing, throttling, and response transfer can all affect an HTTP
   duration. Google defines no conversion from any of those timings to regional
   network RTT.

Global `SERVICE.googleapis.com` endpoints are even less suitable: Google says
they terminate TLS as close to the client as possible. Pinging, opening TCP
connections to, or issuing intentionally failing requests against either
global or regional API endpoints would invent an unsupported probe contract
and must not be a product behavior. Private regional endpoints additionally
require Private Service Connect configuration and are bound by VPC routing and
policy. [Regional and multi-regional endpoint
overview](https://cloud.google.com/docs/security/compliance/about-regional-endpoints)

## Other provider tools do not fill the gap

- Connectivity Tests can actively analyze only supported paths between
  selected endpoints. For a non-Google destination it measures to the Google
  network edge, and configuration analysis alone does not represent current
  data-plane health. [Connectivity Tests
  overview](https://cloud.google.com/network-intelligence-center/docs/connectivity-tests/concepts/overview)
- Cloud Network Insights performs synthetic measurements from installed
  Monitoring Points. Google documents host, control-plane, DNS, NTP, firewall,
  and probe-protocol requirements; a Compute Engine Monitoring Point itself is
  deployed as a VM. [Add Monitoring
  Points](https://cloud.google.com/network-intelligence-center/docs/cloud-network-insights/add-monitoring-points)
- Public uptime checks originate from a small set of Google checker locations,
  require an uptime-check configuration and an existing public target, and use
  HTTP, HTTPS, or TCP. Their latency is checker-to-target service latency, not
  current-client-to-region RTT. [Create public uptime
  checks](https://cloud.google.com/monitoring/uptime-checks)

None provides an authoritative, non-provisioning, current-client-to-all-regions
measurement contract.

## Safe explicit latency-input alternatives

Until a provider contract exists, latency must remain optional evidence. A
missing value is `unknown`; it must not be replaced with geographic distance,
neighbor-region data, API request timing, or an all-project VM-to-VM metric.
The following source kinds can be accepted as explicit inputs without claiming
that the Cloud Quotas manager measured them:

| Source kind | Minimum provenance | Safe interpretation |
| --- | --- | --- |
| `provider-aggregate` | Exact Google Cloud region, source city/geographic region/country, network tier, median RTT, interval, retrieval time, and Performance Dashboard source link. | Typical historical traffic for that geography/region cohort; not the current client. |
| `representative-client-measurement` | Exact region and operator-owned target, source label at the operator's chosen granularity, protocol, topology/network tier, sample count, statistic, observed interval, and collection tool. | Measured path from that representative client to that specific existing target. |
| `existing-workload-telemetry` | Exact region and existing workload identity, telemetry source (`vm_flow`, VPC Flow Logs, load-balancer metric, or application telemetry), client cohort, statistic, and interval. | Observed traffic for that workload and cohort; not an undeployed-region estimate. |
| `operator-estimate` | Exact region, source location, method, assumptions, and creation time. | Planning estimate only. Never label as measured RTT. |
| `requirement` | Exact region or region set, threshold/statistic, workload context, and owner. | Operator constraint used to filter or annotate candidates; not an observation. |

Google's region-selection guidance explicitly recommends representative-user
measurements, existing access logs, existing Google Cloud traffic telemetry,
or rough distance-based estimates for new workloads. It also says that direct
inter-region measurement requires test instances or a third-party source.
Those options validate explicit import, but they do not authorize this product
to provision targets, probe third-party hosts, or trust an external result
without its own source contract. [Compute Engine region-selection best
practices](https://cloud.google.com/solutions/best-practices-compute-engine-region-selection)

Any imported record should exclude client IP addresses, credentials, request
payloads, and endpoint secrets. The product can preserve a user-supplied coarse
source label and the measurement methodology without retaining identifying
network data. Values from different source kinds, tiers, protocols, target
types, statistics, or intervals are not directly comparable unless an operator
explicitly supplies a normalized dataset under one methodology.

## Decision boundary for product planning

The research supports three facts for the parent Wayfinder decision:

1. There is no official universal active-probe contract to implement.
2. Performance Dashboard aggregate city-to-region RTT is a legitimate optional
   provider signal when available, but it needs its own provenance and cannot
   be presented as current-client latency.
3. Exact client latency needs an explicit operator-provided measurement,
   existing-workload telemetry, or a separately authorized future measurement
   system with a controlled target and methodology.

Choosing whether to expose Performance Dashboard evidence, accept imported
records, define a normalization policy, or omit latency from the first release
remains a product decision; this research does not choose among those options.
