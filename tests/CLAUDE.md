# Tests

`ATLAS_ROOT=. uv run pytest -q`: one check per node over one fixture transcript (`tests/fixtures/`), no model or network calls (model paths are monkeypatched or exercised through their degraded branches). CI runs the same command on every push and pull request. When logic changes, add or adjust one test named after the behaviour it protects; no fixtures beyond the one file and no per-function suites.
<!-- generated:tests -->
- `test_ingest_reads_filename_and_header`
- `test_ingest_project_is_the_engagement_code`: A report header 'Project: PRJ-BASALT (Reputational due diligence)' must not create a second engagement in the filters
- `test_detect_reuses_unchanged_documents`: A document whose text hash matches last run's metadata is not sent to the model again
- `test_presidio_finds_contact_details`
- `test_union_merges_overlaps_and_flags_review`
- `test_union_splits_a_clause_around_a_name`
- `test_resolve_links_contributor_to_salesforce_and_initials_to_review`
- `test_roles_decide_who_becomes_an_entity`
- `test_a_same_name_at_another_organisation_is_queued_not_merged_in_silence`: The exact-name tier linked two people with one name and no organisation check, silently: on the demo corpus a central
- `test_a_provisional_link_never_hardens_into_an_alias`: 'Tadashi Kurosawa' was linked to Takeshi in the review band, became an alias, and every later mention then matched
- `test_a_contributor_who_has_left_is_marked_not_hidden`: Salesforce Status__c was not read at all, so a leaver looked as reachable as anyone. Their interviews stay in the
- `test_qa_gate_flags_leaks_and_auto_approves_clean_docs`
- `test_redact_replaces_pii_consistently_and_fills_vault`
- `test_human_decisions_survive_a_rerun`: What a person decided is re-applied when the pipeline rebuilds the tables: a rejected identity link, a restored span, a manual redaction
- `test_manual_redaction_keeps_organisations_and_reuses_tokens`: A reviewer's redaction is whole-word, never cuts into an organisation name, reuses the token of a person the document already holds, and can be undone
- `test_surname_alone_links_to_the_one_person_in_the_document`: A rules-only 'Ogata' after 'Masaru Ogata' is that person; a surname three people share stays unlinked and informational
- `test_tokens_survive_a_new_document`: A person keeps their token when the corpus changes underneath them. Numbering followed order of first encounter, so one
- `test_tokens_survive_renumbered_entity_ids`: A contributor added to the Salesforce export shifts every entity id after it (ids are positional), and seeding the
- `test_graph_has_interviewed_edge_with_provenance`
- `test_chunking_by_speaker_turns`
- `test_store_writes_are_atomic`: A write lands whole or not at all: a failure mid-write leaves the previous file untouched and no temp file behind
- `test_store_round_trip_and_cache`: A serving table written as Delta is what the cache serves; the same code path OneLake uses in production
- `test_store_memoises_onelake_working_files`: On OneLake a working file is fetched once per process: a second read costs no round trip, a write or a full cache refresh fetches it again
- `test_eventhouse_kql_shape`: The two KQL rules that broke production once: dates compare as datetimes, and the project column is bracketed
- `test_eventhouse_honours_a_single_index_mode`: A zero weight drops that leg, and a keyword-only query must not fall back to the dense leg for its tie-break:
- `test_subject_lookup_is_logged_without_the_name`: A name lookup is the one bypass of pseudonymisation the right of access needs; it leaves a row with who, why and the entity ids, never the name
- `test_research_traverses_the_network`: Shortest chains between two contributors run through the sources they share, each step dated; a neighbourhood lists who sits at each hop
- `test_verify_keeps_the_order_it_was_given`: ask() checks the headline by appending it as the last unit and reading it back off the end, so verify must return
- `test_plan_tolerates_nulls_from_the_model`: A null or a non-numeric depth from the planner model falls back to the field's default instead of failing the plan
- `test_sign_in_returns_to_a_local_path_only`: After sign-in the app goes back to the page that was asked for, never to another host (an open redirect is a phishing hop)
- `test_a_question_left_mid_briefing_is_rejoined_not_asked_again`: Switching task dropped the response, so the desk asked the question again on the way back: a second set of model
- `test_a_region_in_the_query_filters_and_is_not_ranked_on`: 'compliance at a gulf bank' returned a US pharma lawyer whose topics happened to say 'central bank supervision
- `test_home_overview_counts_reach_holders_and_pace`: Home's coverage counts sources per region and sector with the contributors who hold them and the last contact, reach decays with age; pace counts interviews per month over 24 months; a contributor is quiet after a year
<!-- /generated -->
