class UpcLookupProvider:
    @property
    def name(self) -> str:
        return self.__class__.__name__

    def lookup(self, brand: str, product_name: str, sku: str, pack_size: str, case_pack: str) -> dict:
        raise NotImplementedError
