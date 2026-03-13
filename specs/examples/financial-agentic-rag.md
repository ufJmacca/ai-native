# Financial Agentic RAG Analyst

## Summary

Build a production-shaped financial research agent that answers investing and financial-statement questions using two provided enterprise data sources:

- A vector database containing unstructured filings and disclosure text.
- A SQL database containing structured financial statement data.

The agent should run on Google ADK, use agentic routing to decide when to call vector retrieval, NL2SQL, or both, and return A2UI-compatible responses so clients can render charts, KPI cards, and tables directly from SQL-backed results.

## Problem

Financial analysts often need both numeric precision and narrative explanation when evaluating a company. A purely retrieval-based system can surface useful context but cannot reliably calculate multi-period trends or ratios from structured statement data. A purely SQL-based system can answer numeric questions but cannot explain the underlying business drivers, footnotes, or management commentary that give the numbers meaning.

We need an agent that can:

- Understand natural-language investing questions.
- Retrieve relevant disclosure text from a vector database.
- Generate and execute safe read-only SQL against a financial statement warehouse.
- Combine numeric findings with cited narrative evidence.
- Return structured A2UI payloads that visualize SQL-backed data for downstream clients.

## Goals

- Provide grounded answers to financial and investing questions using financial statements and disclosures.
- Support hybrid reasoning across unstructured and structured data in a single agent flow.
- Use SQL as the source of truth for numeric outputs and visualizations.
- Use vector retrieval as the source of truth for narrative evidence and citations.
- Expose A2UI-compatible outputs so frontends can render visual components without custom parsing.
- Make the agent auditable by preserving query routing, SQL provenance, and source citations.

## Users

- Equity research analysts comparing company performance across periods.
- Portfolio managers reviewing company fundamentals before making investment decisions.
- Internal strategy and finance teams analyzing public company filings.
- Platform engineers embedding a financial research agent into internal tools or client products.

## Primary User Stories

- As an analyst, I want to ask a plain-English question about revenue, margins, debt, or cash flow and get a precise answer with citations.
- As an investor, I want charts and tables to be generated automatically from structured data without manually exporting results.
- As a research user, I want narrative explanations tied to the specific filing sections that support the answer.
- As a platform engineer, I want the agent to expose deterministic tool boundaries so routing, debugging, and testing are manageable.
- As a compliance-minded operator, I want full traceability from final answer to SQL query and retrieved excerpts.

## Assumptions

- The vector database is already provisioned and contains chunked, embedded, and indexed company disclosures.
- The SQL database is already provisioned and contains normalized financial statement data.
- Runtime authentication, tenant isolation, and network controls are explicitly configured for each deployment environment.
- The SQL schema includes company identifiers, fiscal periods, statements, line items, units, and enough metadata to disambiguate reported values.
- The first release is read-only and does not write to any database or invoke external brokerage or trading systems.
- The initial focus is public-company financial analysis rather than private company data, macroeconomic forecasting, or alternative data.

## In-Scope Data

### Vector Database

- Annual reports and quarterly reports.
- MD&A sections.
- Financial statement footnotes.
- Risk factors relevant to financial interpretation.
- Earnings call excerpts or transcripts if already indexed.
- Metadata such as company, filing type, filing date, fiscal period, section name, and document location.

### SQL Database

- Income statement data.
- Balance sheet data.
- Cash flow statement data.
- Segment or business-unit level metrics if present.
- Company dimension tables.
- Fiscal calendar and period dimension tables.
- Units, scaling metadata, and restatement flags if present.

## Out of Scope

- Real-time pricing, market microstructure, or intraday trading signals.
- Autonomous trade execution or order routing.
- Portfolio construction or rebalancing recommendations.
- Forecasting future earnings unless explicitly derived from user-provided assumptions.
- Write access to the SQL database, vector database, or any downstream system.
- Non-financial domains in the first release.

## Product Behavior

### High-Level Flow

1. The user submits a natural-language question.
2. The ADK orchestrator classifies the request as one of:
   - `vector_only`
   - `sql_only`
   - `hybrid`
   - `clarification_required`
   - `unsupported`
3. If needed, the agent resolves company names, ticker aliases, time ranges, and metric intent.
4. The orchestrator calls the appropriate tools:
   - Vector retrieval tool for qualitative evidence.
   - Schema/context tool for SQL planning.
   - NL2SQL generation tool.
   - SQL validation and execution tool.
   - Answer synthesis tool.
   - A2UI payload builder.
5. The agent returns:
   - A concise natural-language answer.
   - Citations for retrieved narrative evidence.
   - SQL-backed evidence for numeric claims.
   - A2UI-compatible UI objects for tables, KPI cards, or charts when quantitative output is relevant.

### Routing Rules

- Use `vector_only` for questions like "What risks did management mention around margin pressure?"
- Use `sql_only` for questions like "Show revenue growth over the last eight quarters."
- Use `hybrid` for questions like "Show gross margin trend and explain the main drivers management discussed."
- Use `clarification_required` when company, period, metric, or comparison basis is ambiguous.
- Use `unsupported` for requests involving trading, non-approved data sources, or unavailable analytics.

## Agent Architecture

### Google ADK Orchestration

The implementation should use Google ADK as the primary framework for:

- Session and request handling.
- Tool registration and invocation.
- Multi-step planning.
- State passing between retrieval, SQL, and synthesis steps.
- Structured response generation.

### Recommended Agent Components

- Orchestrator agent:
  Classifies intent, manages tool sequencing, and decides whether to ask for clarification.
- Retrieval agent or tool:
  Queries the vector database and returns relevant excerpts plus metadata.
- SQL planner:
  Maps the question to required entities, time windows, statements, dimensions, and calculations.
- NL2SQL generator:
  Produces a candidate SQL query using approved schema context only.
- SQL validator:
  Verifies the query is read-only, limited to approved tables, and consistent with the intended metric.
- SQL executor:
  Runs validated SQL and returns typed result data.
- Finance synthesis agent:
  Combines SQL findings and retrieved text into a coherent answer while preserving provenance.
- A2UI renderer:
  Converts SQL-backed result sets into A2UI-compatible structures.

## Functional Requirements

### FR0. Self-Hosted Runtime Security Controls

- The runtime must support explicit authentication modes for ingest and read APIs:
  - API key authentication.
  - Bearer token or JWT authentication.
  - Optional no-auth mode only for local development and explicitly disabled by default in non-local environments.
- Authentication mode must be configurable per environment and surfaced in operator-facing configuration docs.
- Read and ingest routes must fail closed when auth is misconfigured.
- The runtime must support tenant and project isolation controls when enabled via configuration:
  - Enforce tenant and project scope on every read/write operation.
  - Deny cross-tenant and cross-project access by default.
  - Include effective tenant/project scope in audit records.
- Ingest and read APIs must validate requests before execution, including:
  - Required field checks and type validation.
  - Allowed-value and format validation for tenant/project identifiers.
  - Payload size limits with deterministic rejection behavior when limits are exceeded.
- The runtime must emit audit logs for both writes and reads, including:
  - Timestamp, request identifier, caller identity (or local-dev no-auth marker), and tenant/project scope.
  - Operation type, resource target, and outcome (allow/deny/error).
  - Sufficient metadata to trace why a request was accepted or rejected without logging raw secrets.
- Sensitive values must be protected end-to-end:
  - Secrets are sourced from environment variables or a managed secret store.
  - Secrets are never returned in API responses.
  - Sensitive payload fields are redacted before persistence or long-term log storage.

### FR1. Query Understanding

- The system must identify:
  - Company or company set.
  - Time period or period range.
  - Metric or statement concept.
  - Desired comparison type such as trend, ranking, ratio, peer comparison, or driver analysis.
  - Whether narrative explanation is requested or implied.
- The system must normalize common aliases such as ticker symbols, abbreviated company names, and finance shorthand.
- The system must detect ambiguity such as "last year" when a company has a non-calendar fiscal year and either resolve it using the fiscal calendar or ask a clarification question.

### FR2. Vector Retrieval

- The system must retrieve top relevant disclosure chunks using semantic search over the provided vector database.
- Retrieved passages must include source metadata at minimum:
  - Company.
  - Filing type.
  - Filing date.
  - Fiscal period.
  - Section name or equivalent location marker.
- The system should bias retrieval toward the same company and time range identified in the user question.
- The system must support citation-ready excerpts for answer grounding.
- The system must distinguish raw retrieved text from model-generated interpretation.

### FR3. NL2SQL

- The system must generate read-only SQL using only approved schemas and tables.
- SQL generation must use schema context rather than hallucinated table names or columns.
- The system must support:
  - Single-company trend queries.
  - Multi-period comparisons.
  - Cross-company ranking queries.
  - Common derived metrics such as YoY growth, QoQ growth, gross margin, operating margin, free cash flow, debt-to-equity, and working-capital changes when derivable from the schema.
- The system must reject unsafe or unsupported SQL patterns including:
  - DML or DDL statements.
  - Access to non-approved schemas.
  - Cartesian joins without justification.
  - Queries that omit required period or company constraints when the user intent is specific.
- The final answer must preserve the executed SQL or a safe audit representation for debugging and traceability.

### FR4. Structured Result Handling

- SQL result sets must preserve column names, units, period labels, and any dimensional breakdowns.
- The agent must detect when returned values require scaling normalization such as raw units versus millions.
- If the result set is empty, the system must return a clear explanation rather than fabricating an answer.
- If multiple comparable metrics exist in the schema, the answer must state which metric definition was selected.

### FR5. Hybrid Answer Synthesis

- For hybrid questions, the answer must combine:
  - Numeric evidence from SQL.
  - Narrative evidence from vector retrieval.
  - A final explanation that connects the two without overstating causality.
- The system must clearly separate:
  - Reported facts.
  - Calculated metrics.
  - Model interpretation.
- If narrative evidence conflicts with numeric patterns, the answer must state the mismatch explicitly.

### FR6. A2UI-Compatible Responses

- Quantitative responses must include structured A2UI-compatible output rather than only prose.
- The first release must support at minimum:
  - KPI cards for point-in-time metrics.
  - Tables for detailed result inspection.
  - Line charts for time-series trends.
  - Bar charts for comparisons across companies, segments, or periods.
- Each visualization payload must be traceable to the SQL result set used to create it.
- The response should include enough metadata for a client to label axes, periods, units, titles, and legends correctly.
- Narrative-only questions may return prose and citations without a chart payload.
- Hybrid questions should return both narrative content and at least one SQL-backed A2UI visual when quantitative evidence is central to the answer.

### FR7. Citation and Provenance

- Every narrative claim derived from filings must point to retrieved source excerpts.
- Every quantitative claim must be traceable to SQL results.
- The final answer should identify:
  - Company.
  - Fiscal period context.
  - Statement basis.
  - Units.
  - Whether values are reported or derived.
- Debug or audit mode should expose routing decisions, retrieved document identifiers, and executed SQL.

### FR8. Clarification and Failure Handling

- The system must ask follow-up questions when the request is materially ambiguous.
- The system must return a clear failure state when:
  - The company cannot be resolved.
  - The requested period is not available.
  - The metric is unsupported by the schema.
  - Retrieved evidence is insufficient.
  - The SQL query fails validation.
- The system must refuse unsupported requests such as:
  - Buy or sell recommendations framed as direct investment advice.
  - Trade execution.
  - Fabrication of missing financial statement values.

## Finance-Specific Capabilities

- Trend analysis across quarterly or annual periods.
- Variance analysis between periods.
- Cross-company ranking based on financial statement metrics.
- Segment contribution analysis when segment data exists.
- Ratio analysis based on statement line items.
- Cash flow decomposition using operating cash flow, investing cash flow, financing cash flow, and capital expenditures.
- Narrative explanation using MD&A, footnotes, and risk disclosures.
- Detection of simple tensions between narrative claims and underlying numeric trends.

## Example Questions

- What drove Microsoft revenue growth in the last four quarters, and which segments contributed the most?
- Show Nvidia gross margin over the last eight quarters and explain any major changes discussed in management commentary.
- Compare Apple operating cash flow, capital expenditures, and free cash flow for the last three fiscal years.
- Which companies in my coverage universe showed the largest year-over-year increase in long-term debt last year?
- Did Tesla inventory grow faster than revenue over the last six quarters?
- Summarize the main risks management mentioned around margin pressure and pair that summary with a margin trend chart.
- Show Amazon operating income by segment for the last four quarters and explain which business lines improved the most.
- Which semiconductor companies had the strongest free cash flow growth last fiscal year, and how did management explain capital intensity?

## Example Expected Behaviors

### Example 1. SQL-Only

Question: "Show Apple free cash flow for the last three fiscal years."

Expected behavior:

- Resolve Apple to the correct company entity.
- Query operating cash flow and capital expenditures from the SQL database.
- Calculate free cash flow using the agreed schema rule.
- Return a short answer with period-by-period values.
- Return an A2UI line chart and table.
- Include units and fiscal-year labels.

### Example 2. Vector-Only

Question: "What risks did management highlight related to margin pressure?"

Expected behavior:

- Retrieve relevant MD&A or risk factor excerpts.
- Return a narrative summary grounded in citations.
- Avoid inventing numeric trends if SQL was not required.
- Omit chart output unless a quantitative follow-up is requested.

### Example 3. Hybrid

Question: "Show Nvidia gross margin trend over the last eight quarters and explain the biggest drivers."

Expected behavior:

- Query revenue and cost of revenue from SQL or use gross margin if already modeled.
- Build a time-series visualization in A2UI format.
- Retrieve filing excerpts or earnings commentary that discuss pricing, product mix, supply constraints, or inventory effects.
- Produce a final answer linking the chart trend to cited narrative evidence.

## Response Requirements

- Answers must be concise first, then evidence-backed.
- Numeric claims must specify units such as USD, millions, billions, or percentages.
- Period references must use explicit fiscal labels where possible.
- The response should not imply causation unless the cited evidence supports it.
- If the answer is partly inferred, the inference must be labeled clearly.
- The default response should include:
  - Direct answer.
  - Key supporting metrics.
  - Citations.
  - A2UI output when SQL-backed visualization is relevant.

## Non-Functional Requirements

- Read-only access to approved data sources only.
- P95 latency should be acceptable for interactive research workflows, with the understanding that hybrid queries may take longer than simple retrieval.
- The system should degrade gracefully when one data source is unavailable.
- SQL and retrieval steps must be observable through structured logs or trace metadata.
- The implementation should support deterministic test fixtures for finance questions and expected outputs.
- The system should be designed so additional metrics, schemas, or chart types can be added without reworking the orchestration layer.

## Safety and Compliance

- Restrict the agent to approved schemas, tables, and retrieval indexes.
- Prevent SQL mutation statements and access escalation.
- Avoid presenting speculative investment advice as fact.
- Label analysis as informational research support rather than autonomous decision-making.
- Preserve enough provenance for internal review and audit.
- Ensure unsupported or low-confidence responses fail safely.

## Testing Requirements

- Unit tests for routing between vector, SQL, hybrid, clarification, and refusal flows.
- Unit tests for metric resolution and fiscal-period interpretation.
- Unit tests for NL2SQL validation against allowed and disallowed queries.
- Integration tests for representative financial questions using fixture data.
- Tests verifying that A2UI payloads are emitted for SQL-backed chartable responses.
- Tests verifying citations are present for narrative answers.
- Regression tests for common finance calculations such as:
  - YoY revenue growth.
  - Gross margin.
  - Operating margin.
  - Free cash flow.
  - Debt-to-equity.
- Tests for failure paths such as ambiguous company names, missing periods, or absent schema fields.

## Delivery Artifacts

- An ADK-based agent implementation with clear tool boundaries.
- Connector configuration for the provided vector and SQL backends.
- A README explaining setup, local execution, sample prompts, and known limitations.
- Test coverage for core routing, NL2SQL, retrieval grounding, and A2UI output generation.
- Example prompts and example responses suitable for demos or evaluation.

## Acceptance Criteria

- Accepts natural-language questions involving trends, comparisons, rankings, ratio analysis, and disclosure lookups from financial statements.
- Correctly routes each request to vector retrieval, SQL, or a hybrid flow.
- Generates safe read-only SQL against the provided schema and refuses unsupported query patterns.
- Produces accurate numeric responses based on SQL results without inventing missing values.
- Produces narrative explanations grounded in retrieved filing excerpts with source attribution.
- Emits A2UI-compatible payloads for SQL-backed responses so a client can render KPI cards, tables, and charts.
- Preserves company, period, unit, and provenance context in every answer.
- Returns useful clarification requests or failure messages when intent or data is ambiguous.
- Includes automated tests for core finance scenarios and edge cases.
- Is documented well enough that another engineer can run, test, and extend the system.

## Open Questions

- Which financial statement schema should be treated as canonical if multiple normalized marts exist?
- Which exact A2UI component subset should be guaranteed in v1 beyond cards, tables, line charts, and bar charts?
- Should earnings call transcript retrieval be enabled in the first release or deferred until filing-based workflows are stable?
- How should peer universes such as "my coverage universe" be provided: explicit list, user profile, or saved workspace filter?
- Should the system expose the executed SQL to end users by default or only in a debug view?
