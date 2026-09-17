"""PromQL builders for project dashboards. No request-type selectors."""
import json
from metric_scope import apply_scope

SOURCE = "job,instance,environment"


class Queries:
    def __init__(self, environment, job, extra="", origin=None):
        self.environment = environment
        self.labels = 'environment=' + json.dumps(environment) + ',job=' + json.dumps(job) + extra
        self.origin = origin

    def s(self, name, extra=""):
        return apply_scope(name + "{" + self.labels + extra + "}")

    def source(self, window="$__interval"):
        up = self.s("up")
        result = (
            f'({up} == 1) and (time() - timestamp({up}) < 15)'
            f' and (min_over_time({up}[{window}]) == 1)'
            f' and (count_over_time({up}[{window}]) >= ({window} / 5))'
        )
        if self.origin:
            origin = self.s(self.origin)
            result += (
                f' and on({SOURCE}) ((changes({origin}[{window}]) == 0)'
                f' and (count_over_time({origin}[{window}]) >= ({window} / 5))'
                f' and (time() - {origin} >= {window})'
                f' and (time() - timestamp({origin}) < 15))'
            )
        return result

    def good(self, selector, counter=False):
        result = (
            f'(time() - timestamp({selector}) < 15)'
            f' and (count_over_time({selector}[$__interval]) >= ($__interval / 5))'
            f' and on({SOURCE}) ({self.source()})'
        )
        if counter:
            result += (
                f' and (count_over_time({selector}[1m]) >= 12)'
                f' and (resets({selector}[1m]) == 0)'
                f' and (resets({selector}[$__interval]) == 0)'
                f' and on({SOURCE}) ({self.source("1m")})'
            )
        return result

    def gauge(self, name, extra=""):
        s = self.s(name, extra)
        return f'({s} and ({self.good(s)}))'

    def rate(self, name, extra="", group=None):
        s = self.s(name, extra)
        good = self.good(s, counter=True)
        value = f'(rate({s}[1m]) and ({good}))'
        if group:
            bad = f'(count_over_time({s}[1m]) unless ({good}))'
            return f'(sum by({group}) ({value})) unless on({group}) (count by({group}) ({bad}))'
        return value

    def histogram(self, name, bounds, quantile, group, origin=None):
        """Reject missing/inconsistent buckets before Prometheus can repair them.

        Compare adjacent cumulative buckets both at the evaluation timestamp and
        as window rates. Validate all observed count/bucket series independently;
        an invalid source invalidates its aggregation group.
        """
        bucket, count = self.s(name + "_bucket"), self.s(name + "_count")
        family = self.s("", ',__name__=~' + json.dumps(name + "_(bucket|count)"))
        good = self.good(family, counter=True)
        errors = [f'(count_over_time({family}[1m]) unless ({good}))']
        errors.append(f'(count without(le) ({bucket}) != {len(bounds)})')
        infinite = self.s(name + "_bucket", ',le="+Inf"')
        errors.append(f'({infinite} != ignoring(le) {count})')
        for lower, upper in zip(bounds, bounds[1:]):
            a = self.s(name + "_bucket", ",le=" + json.dumps(lower))
            b = self.s(name + "_bucket", ",le=" + json.dumps(upper))
            errors.append(f'({a} > ignoring(le) {b})')
            errors.append(f'(rate({a}[1m]) > ignoring(le) rate({b}[1m]))')
        # A missing complete count or bucket family must also invalidate a source.
        errors += [
            f'({count} unless ignoring(le) {bucket})',
            f'({bucket} unless ignoring(le) {count})',
        ]
        if origin:
            marker = self.s(origin)
            errors.append(
                f'({count} unless ignoring(request_scope) ((changes({marker}[1m]) == 0)'
                f' and (time() - {marker} >= 60)'
                f' and (changes({marker}[$__interval]) == 0)'
                f' and (count_over_time({marker}[1m]) >= 12)'
                f' and (count_over_time({marker}[$__interval]) >= ($__interval / 5))))'
            )
        invalid = " or ".join(errors)
        return (
            f'histogram_quantile({quantile}, sum by(le,{group}) (rate({bucket}[1m])))'
            f' unless on({group}) (count by({group}) ({invalid}))'
            f' and on({group}) (sum by({group}) (rate({count}[1m])) > 0)'
        )

    def histogram_quantiles(self, name, bounds, group, origin=None):
        """Three quantiles, one validity mask; no changes to source semantics."""
        expression = self.histogram(name, bounds, .5, group, origin)
        prefix = 'histogram_quantile(0.5, '
        assert expression.startswith(prefix)
        expression = 'histogram_quantiles("perses_quantile", 0.5, 0.95, 0.99, ' + expression[len(prefix):]
        # Give legends their original names without exposing numeric phi values.
        for value, label in (("0.5", "P50"), ("0.95", "P95"), ("0.99", "P99")):
            expression = f'label_replace(({expression}), "perses_quantile", "{label}", "perses_quantile", "{value.replace(".", "[.]")}")'
        return expression



def ratio(numerator, denominator, labels):
    return f'100 * (({numerator}) / on({labels}) (({denominator}) > 0))'
