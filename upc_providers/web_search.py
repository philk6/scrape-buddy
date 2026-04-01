for provider in providers:
    try:
        result = provider.lookup(brand, product_name, sku, pack_size, case_pack)
        if result and result.get('exact_match'):
            return result
        results.append(result)
    except Exception as e:
        print(f"Provider {provider.name} failed: {e}")
print(results)
def get_best_match(results, brand, product_name, sku):
    #Find exact match first
    for r in results:
        if r and r.get('exact_match'):
            return r
    # Filter based on brand and name, re-score by relevance
    # For now, just return the first result
    return results[0] if results else {}
