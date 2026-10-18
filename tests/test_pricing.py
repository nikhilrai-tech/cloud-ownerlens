import json

from ownerlens.pricing import PriceBook, os_for_instance


class FakePricing:
    def __init__(self, usd):
        self.usd, self.calls = usd, 0

    def get_products(self, **kwargs):
        self.calls += 1
        fields = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
        assert fields["location"] == "Asia Pacific (Mumbai)" and fields["operatingSystem"] == "Linux"
        item = {"terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": self.usd}}}}}}}
        return {"PriceList": [json.dumps(item)] if self.usd is not None else []}


class FakeSession:
    def __init__(self, fake):
        self.fake = fake

    def client(self, name, region_name=None):
        assert name == "pricing" and region_name == "us-east-1"
        return self.fake


def test_monthly_price_and_cache():
    fake = FakePricing("0.0448")
    book = PriceBook(FakeSession(fake))
    assert book.monthly("t3.medium", "ap-south-1") == 32.70
    assert book.monthly("t3.medium", "ap-south-1") == 32.70
    assert fake.calls == 1


def test_unknown_price_is_none_not_a_guess():
    assert PriceBook(FakeSession(FakePricing(None))).monthly("zz.huge", "ap-south-1") is None
    assert PriceBook(FakeSession(FakePricing("0.1"))).monthly("t3.medium", "mars-1") is None


def test_os_mapping():
    assert os_for_instance("Linux/UNIX") == "Linux" and os_for_instance(None) == "Linux"
    assert os_for_instance("Windows") == "Windows"
    assert os_for_instance("Red Hat Enterprise Linux") is None
