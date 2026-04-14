from services.applier import register
from services.appliers._navent_base import NaventApplier


@register
class ZonaJobsApplier(NaventApplier):
    platform_name = "zonajobs"
    cv_profile_name = "local"
