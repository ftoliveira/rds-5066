"""Tests for types.py — ServiceType dataclass changes."""

from src.stypes import ServiceType


class TestServiceType:
    def test_default_values(self):
        st = ServiceType()
        assert st.transmission_mode == 2  # both ARQ and NON_ARQ
        assert st.delivery_confirmation == 0
        assert st.delivery_order is False
        assert st.extended is False
        assert st.min_retransmissions == 0

    def test_backward_compat_arq(self):
        assert ServiceType(transmission_mode=0).arq is True
        assert ServiceType(transmission_mode=0).non_arq is False

    def test_backward_compat_non_arq(self):
        assert ServiceType(transmission_mode=1).arq is False
        assert ServiceType(transmission_mode=1).non_arq is True

    def test_backward_compat_both(self):
        assert ServiceType(transmission_mode=2).arq is True
        assert ServiceType(transmission_mode=2).non_arq is True

    def test_backward_compat_expedited(self):
        assert ServiceType(extended=True).expedited is True
        assert ServiceType(extended=False).expedited is False

    def test_frozen(self):
        st = ServiceType()
        import pytest
        with pytest.raises(AttributeError):
            st.transmission_mode = 1  # type: ignore

    def test_from_decoded_dict(self):
        d = {
            'transmission_mode': 1,
            'delivery_confirmation': 2,
            'delivery_order': True,
            'extended': True,
            'min_retransmissions': 7,
        }
        st = ServiceType(**d)
        assert st.transmission_mode == 1
        assert st.delivery_confirmation == 2
        assert st.delivery_order is True
        assert st.extended is True
        assert st.min_retransmissions == 7
