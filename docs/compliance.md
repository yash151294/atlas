# Compliance design: how the pipeline meets UK GDPR, and what an operator would still have to decide

An engineering view for the DPO conversation, not legal advice, written for a hypothetical UK firm that runs expert interviews through contractors ("the firm"). Every control below exists in the MVP and can be opened on the Compliance page.

## What is personal data here, and what happens to it

| Data | Where it comes from | What the pipeline does | Where it ends up |
|---|---|---|---|
| Source's name and name variants | transcript header, body, filename | replaced by one stable token per person in every retained copy | vault (token → name), own retention clock |
| Source's professional descriptor (role, organisation, sector, region, seniority) | extraction pass | kept next to the token; this is what the network and the research desk use | retained copy legend, `gold_sources` |
| Professionally described third parties (a founder, a regulator counterpart) | extraction pass, role = third_party | name tokenised; descriptor and relationship kept; becomes a second-degree node | vault, `gold_people` |
| Private individuals (family, friends, assistants, anyone named in a personal context) | extraction pass, role = private | name tokenised; nothing kept; never an entity, never in the graph | vault only, per document |
| Contributor's name | filename, header, Salesforce | tokenised in retained copies; shown by name in the network (the firm's own contractor, Salesforce is the record) | vault, `gold_contributors` |
| Phones, emails, addresses, dates of birth, ID and financial details, family details, nationality | both detectors | always replaced | vault |
| Organisations, places, job titles, project codenames | extraction pass | kept (not personal data) | retained copy |
| Raw file and raw text | SharePoint | kept until the retained copy is approved plus N days, then deleted by the retention job | bronze, then gone |

## Principle by principle

**Lawful basis and purpose limitation.** The retained corpus and the network exist to re-use insight the firm already lawfully collected, for the same purpose (client engagements). The likely basis is legitimate interests for source and third-party business-contact records, with a balancing test, and contract plus legitimate interests for contributors. Re-using one engagement's transcripts on a later, different engagement is a change of purpose that needs the Art. 6(4) compatibility reasoning written down. The contributor agreement and whether sources know they are recorded decide the default identity policy (`identity_policy` in `conf/parameters.yaml`: full, pseudonym with vault, descriptor only). Engagement scope is a property of the platform (Entra groups and OneLake security on the project column), not an app-level filter; development runs as one named analyst.

**Special category and offence data (Art. 9, 10; DPA 2018 Sch. 1).** Intelligence transcripts state health, political opinion, ethnicity or criminal allegations about people; on the synthetic corpus the gate's second model finds such content in roughly half the documents. Pseudonymisation does not take it outside Art. 9, because a token plus a descriptor is still an identifiable person. The gate lists the categories on every document, adds to the risk score (`weights.special_category`; set it to the threshold to send every such document to a person), and the count is on the Quality page. The firm needs a Schedule 1 condition and an appropriate policy document before retaining such content; the product's job is to know where it is.

**Data minimisation.** The role classification is the minimisation control. Only what is needed to find and re-establish access is kept: a professional descriptor. Private individuals are removed outright. Contact details and special-category-adjacent details never survive into a retained copy. The `descriptor_only` policy goes further and replaces even the organisation with a generalised descriptor written by the model ("former senior compliance executive at a Gulf bank").

**Pseudonymisation (Art. 4(5)) and security (Art. 32).** Tokens are stable per person so the corpus stays readable and joinable; the token-to-identity map is a separate table with its own retention clock, a reason-required reveal, and an access log. In production the vault sits in its own Lakehouse behind OneLake security, and the research desk never reads it. A retained copy without the vault is anonymous to anyone outside the firm and pseudonymous inside.

**Storage limitation.** Two clocks, both enforced by a job and both logged: the raw file is deleted N days after the retained copy is approved; vault rows expire after N days. The Compliance page shows what is due and runs the job; in production it is a scheduled Fabric notebook.

**Accuracy.** Identity links are deterministic first (Salesforce id, name plus organisation), scored second, and a person third; people are never merged on a fuzzy score alone. Every mention keeps its document and offsets, so a wrong link is reversible. A rejected identity question re-runs the document.

**Rights.** Subject access: the Compliance page finds everything held under a name across vault, entities and documents and exports it; the lookup itself is logged with who, the request reference and the entity ids found, never the name. Erasure: vault rows and the entity record are deleted, logged with the request reference; tokens stay in the retained copies and become anonymous, so the corpus stays consistent. A contributor who leaves keeps their edges as history (provenance) and their Salesforce status changes.

**Automated decisions.** Approval by policy is a decision about a document, not about a person, and it is reviewable: every auto-approved document can be opened, its reasons are listed, and a random sample is re-read by a person. No decision about an individual is automated.

**Accountability.** Every model call is logged with model, provider, prompt version hash and cost. Every approval, restore, manual redaction, reveal, erasure and deletion is logged with who and why. The evals snapshot (`uv run python -m atlas.evals.snapshot`) carries the measured numbers the DPIA will quote.

**Processing location and training.** Embeddings, planning, the residual check and answers run on an Azure OpenAI resource in UK South under Microsoft product terms (no training, no sharing with OpenAI, regional processing). Extraction, the one step that reads the raw transcript with names in it, runs on Anthropic's first-party API under no-training commercial terms. It was moved in-tenancy on 13 Sep and moved back the same day: the in-tenancy model cut PERSON recall from 100% to 97% and missed personal guarantees, relatives and addresses, and Azure's content filter blocked an asset-tracing transcript. The intended home is Claude on Microsoft Foundry, same code path, same model, inside the tenancy; it waits only on Anthropic quota in the subscription. Until then the first-party call is an international transfer of the most sensitive form of the data and needs the processing agreement and a UK transfer mechanism (IDTA or Addendum) on file. The provider is stamped on every ledger row.

## Approval at scale, defended

Fifteen thousand documents cannot each be read by a person. The gate is defensible because it is layered and measured:
1. every value the detectors found must be absent from the retained copy (a leak is a hard flag);
2. a rules pass and an independent second model read the retained copy for any surviving name or contact detail;
3. a risk score adds unresolved identity questions, lone-detector spans, sensitive types, a transcript with no identified source, and any degraded (model-less) extraction;
4. below the threshold the document is approved by a named policy; above it a person decides, with the reasons listed;
5. a random sample of approved documents is re-read by a person and the issue rate is reported; the threshold moves if the sample says so;
6. redaction recall is measured against labelled ground truth on every run.

## Are we violating anything by doing this?

On the synthetic corpus, no: every person is fictional. On real data the design is defensible, but only under assumptions that the firm has to confirm, and two things in the current build would be non-compliant as-is on day one:

1. Raw transcripts leave the tenancy for extraction (above). Fix: Claude on Foundry once quota is granted (an env change), or processor terms plus a transfer mechanism for the first-party API in the meantime.
2. Special-category and offence content is retained under a token with a descriptor with no Schedule 1 condition documented. The gate now detects and counts it; the condition and the policy document are the firm's to write before the first real document.

The rest is organisational rather than code, and it is where a DPO will look first:

| Assumption | Why it matters | What settles it |
|---|---|---|
| Pseudonymised copies are still personal data | A role, an organisation and a date single a person out; the vault reduces risk, it does not anonymise. Lawful basis must cover the retained copy, and the corpus after vault expiry may still be personal data (Art. 11 then applies to rights requests). | Say it in the DPIA; use `descriptor_only` where a role is unique. |
| Cross-engagement re-use is a compatible purpose | Transcripts were collected for one client's engagement; searching them for another is a new purpose (Art. 5(1)(b), 6(4)). | Compatibility assessment; contributor agreement and source notice wording. |
| Sources and mentioned professionals are informed | Art. 13 for sources the firm interviews; Art. 14 for people named in passing, with 14(5)(b) (disproportionate effort) documented, not assumed. | Privacy notice via contributors; a documented 14(5)(b) decision for third parties. |
| Legitimate interests covers business-contact records | Sources and professionally described third parties are held as role, organisation, relationship: the business-contact case the ICO treats as lower risk, as long as nothing private is kept (the role classification enforces this). | Legitimate interests assessment on file. |
| Contributor mapping is covered by the contributor agreement | The network is a profile of each contractor's access. | Contract wording. |
| Client contracts permit retention and cross-client search | Deliverables and transcripts are client work product; confidentiality is contractual, separate from GDPR. | Month-one register; engagement flag to exclude or route. |
| A DPIA precedes real data | Large scale, innovative technology, data about third parties, matching Salesforce to transcripts: Art. 35 applies. | DPIA signed off with the DPO. |
| Model-written descriptors are accurate enough | A wrong title or organisation is inaccurate personal data about a real person (Art. 5(1)(d)). | Every descriptor keeps its source passage; correctable on the Review page. |
| The residual rate is accepted | Redaction recall is 94.5% on synthetic text with the second model catching names; family and financial details survive more often. | Label fifty real transcripts before quoting a number. |
| One identity, no app-level roles | Every reveal, approval and erasure is logged under a named user; who may reveal is an authorisation question. | Entra sign-in and a reveal permission in production. |

## DPIA outline
Nature, scope, context and purposes; necessity and proportionality (why pseudonymisation rather than deletion, why descriptors are kept); risks to individuals (re-identification through descriptors, wrong identity merge, residual PII, over-broad access) and the controls above; consultation (DPO, contributor representatives, a client); sign-off and review dates. Residual risk to name honestly: a unique public role plus organisation can identify a person even without a name; that is what the `descriptor_only` dial is for, per engagement.
