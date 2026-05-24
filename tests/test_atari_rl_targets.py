import torch
import torch._dynamo as dynamo

from atari.rl_targets import (
    decode_twohot_symlog,
    symlog,
    symexp,
    twohot_symlog_targets,
)


def test_symlog_roundtrip_finite():
    x = torch.linspace(-25, 25, 101)
    y = symexp(symlog(x))
    assert torch.isfinite(y).all()
    assert torch.allclose(x, y, atol=1e-5)


def test_twohot_shapes_and_mass():
    x = torch.tensor([-100.0, -1.0, 0.0, 1.0, 100.0])
    target = twohot_symlog_targets(x, num_bins=255, low=-25.0, high=25.0)
    assert target.shape == (5, 255)
    assert torch.isfinite(target).all()
    assert torch.allclose(target.sum(dim=-1), torch.ones(5), atol=1e-6)


def test_decode_monotonic_and_clipped():
    x = torch.linspace(-50, 50, 33)
    logits = torch.log(twohot_symlog_targets(x, num_bins=255, low=-25.0, high=25.0) + 1e-8)
    decoded = decode_twohot_symlog(logits, num_bins=255, low=-25.0, high=25.0)
    assert torch.isfinite(decoded).all()
    assert decoded.min() >= -25.0
    assert decoded.max() <= 25.0
    assert torch.all(decoded[1:] >= decoded[:-1] - 1e-5)


def test_decode_has_no_dynamo_graph_breaks():
    def decode(logits):
        return decode_twohot_symlog(logits, num_bins=255, low=-25.0, high=25.0)

    report = dynamo.explain(decode)(torch.randn(4, 255))
    assert report.graph_count == 1
    assert report.graph_break_count == 0
