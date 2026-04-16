def run_pipeline(file_bytes, style):
    result = {}

    # STEP 1: ENGINE (must run first)
    engine_result = run_crosscheck(file_bytes)
    result["engine"] = engine_result

    # STEP 2: PARALLEL TASKS
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        future_verify = pool.submit(run_verification, engine_result)
        future_format = pool.submit(
            process_references,
            raw_reference=engine_result.get("references_text", ""),
            style=style
        )

        verification = future_verify.result()
        formatted = future_format.result()

    result["verification"] = verification
    result["formatted"] = formatted

    # STEP 3: CLAIM SUPPORT (depends on verification)
    claims = build_claim_support_rows({
        **engine_result,
        "online_verification": verification
    })
    result["claims"] = claims

    # STEP 4: ACII
    acii_score = compute_acii(engine_result, verification.get("rows", []))
    result["acii"] = acii_score

    return result
