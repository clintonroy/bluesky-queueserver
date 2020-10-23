import aioredis
import asyncio
import json
import uuid


class PlanQueueOperations:
    """
    The class supports operations with plan queue based on Redis. The public methods
    of the class are protected with ``asyncio.Lock``.

    Parameters
    ----------
    redis_host: str
        Address of Redis host.

    Examples
    --------

    .. code-block:: python

        pq = PlanQueueOperations()  # Redis located at `localhost`
        await pq.start()

        # Fill queue
        await pq.add_plan_to_queue(<plan1>)
        await pq.add_plan_to_queue(<plan2>)
        await pq.add_plan_to_queue(<plan3>)

        # Number of plans in the queue
        qsize = await pq.get_plan_queue_size()

        # Read the queue (as a list)
        queue = await pq.get_plan_queue()

        # Start the first plan (This doesn't actually execute the plan. It is just for bookkeeping.)
        plan = await pq.set_next_plan_as_running()
        # ...
        # Here place the code for executing the plan in dictionary `plan`

        # Again this only shows whether a plan was set as running. Expected to be True in
        #   this example.
        is_running = await pq.is_plan_running()

        # Assume that plan execution is completed, so move the plan to history
        #   This also clears the currently processed plan.
        plan = await pq.set_processed_plan_as_completed(exit_status="completed")

        # We are ready to start the next plan
        plan = await pq.set_next_plan_as_running()

        # Assume that we paused and then stopped the plan. Clear the running plan and
        #   push it back to the queue. Also create the respective history entry.
        plan = await pq.set_processed_plan_as_stopped(exit_status="stopped")
    """

    def __init__(self, redis_host="localhost"):
        self._redis_host = redis_host
        self._uid_dict = dict()
        self._r_pool = None

        self._name_running_plan = "running_plan"
        self._name_plan_queue = "plan_queue"
        self._name_plan_history = "plan_history"

        self._lock = None

    async def start(self):
        """
        Create the pool and initialize the set of UIDs from the queue if it exists in the pool.
        """
        if not self._r_pool:  # Initialize only once
            self._lock = asyncio.Lock()
            async with self._lock:
                self._r_pool = await aioredis.create_redis_pool(f"redis://{self._redis_host}", encoding="utf8")
                await self._queue_clean()
                await self._uid_dict_initialize()

    async def _queue_clean(self):
        """
        Delete all the invalid queue entries (there could be some entries from failed unit tests).
        """
        pq = await self._get_plan_queue()

        def verify_plan(plan):
            # The criteria may be changed.
            return "plan_uid" in plan

        plans_to_remove = []
        for plan in pq:
            if not verify_plan(plan):
                plans_to_remove.append(plan)

        for plan in plans_to_remove:
            await self._remove_plan(plan, single=False)

        # Clean running plan info also (on the development computer it may contain garbage)
        plan = await self._get_running_plan_info()
        if plan and not verify_plan(plan):
            await self._clear_running_plan_info()

    async def _delete_pool_entries(self):
        """
        See ``self.delete_pool_entries()`` method.
        """
        await self._r_pool.delete(self._name_running_plan)
        await self._r_pool.delete(self._name_plan_queue)
        await self._r_pool.delete(self._name_plan_history)
        self._uid_dict_clear()

    async def delete_pool_entries(self):
        """
        Delete pool entries used by RE Manager. This method is mostly intended for use in testing,
        but may be used for other purposes if needed.
        """
        async with self._lock:
            await self._delete_pool_entries()

    def _verify_plan_type(self, plan):
        """
        Check that the plan is a dictionary.
        """
        if not isinstance(plan, dict):
            raise TypeError(f"Parameter 'plan' should be a dictionary: '{plan}', (type '{type(plan)}')")

    def _verify_plan(self, plan):
        """
        Verify that plan structure is valid enough to be put in the queue.
        Current checks: plan is a dictionary, ``plan_uid`` key is present, Plan with the UID is not in
        the queue or currently running.
        """
        self._verify_plan_type(plan)
        # Verify plan UID
        if "plan_uid" not in plan:
            raise ValueError("Plan does not have UID.")
        if self._is_uid_in_dict(plan["plan_uid"]):
            raise RuntimeError(f"Plan with UID {plan['plan_uid']} is already in the queue")

    def _new_plan_uid(self):
        """
        Generate UID for a plan.
        """
        return str(uuid.uuid4())

    def set_new_plan_uuid(self, plan):
        """
        Replaces Plan UID with a new one or creates a new UID.

        Parameters
        ----------
        plan: dict
            Dictionary of plan parameters. The dictionary may or may not have the key ``plan_uid``.

        Returns
        -------
        dict
            Plan with new UID.
        """
        self._verify_plan_type(plan)
        plan["plan_uid"] = self._new_plan_uid()
        return plan

    async def _get_index_by_uid(self, *, uid):
        """
        Get index of a plan in Redis list by UID. This is inefficient operation and should
        be avoided whenever possible. Raises an exception if the plan is not found.

        Parameters
        ----------
        uid: str
            UID of the plans to find.

        Returns
        -------
        int
            Index of the plan with given UID.

        Raises
        ------
        IndexError
            No plan is found.
        """
        queue = await self._get_plan_queue()
        for n, plan in enumerate(queue):
            if plan["plan_uid"] == uid:
                return n
        raise IndexError(f"No plan with UID '{uid}' was found in the list.")

    # --------------------------------------------------------------------------
    #                          Operations with UID set
    def _uid_dict_clear(self):
        """
        Clear ``self._uid_dict``.
        """
        self._uid_dict.clear()

    def _is_uid_in_dict(self, uid):
        """
        Checks if UID exists in ``self._uid_dict``.
        """
        return uid in self._uid_dict

    def _uid_dict_add(self, plan):
        """
        Add UID to ``self._uid_dict``.
        """
        uid = plan["plan_uid"]
        if self._is_uid_in_dict(uid):
            raise RuntimeError(f"Trying to add plan with UID '{uid}', which is already in the queue")
        self._uid_dict.update({uid: plan})

    def _uid_dict_remove(self, uid):
        """
        Remove UID from ``self._uid_dict``.
        """
        if not self._is_uid_in_dict(uid):
            raise RuntimeError(f"Trying to remove plan with UID '{uid}', which is not in the queue")
        self._uid_dict.pop(uid)

    def _uid_dict_update(self, plan):
        """
        Update a plan with UID that is already in the dictionary.
        """
        uid = plan["plan_uid"]
        if not self._is_uid_in_dict(uid):
            raise RuntimeError(f"Trying to update plan with UID '{uid}', which is not in the queue")
        self._uid_dict.update({uid: plan})

    def _uid_dict_get_plan(self, uid):
        """
        Returns a plan with the given UID.
        """
        return self._uid_dict[uid]

    async def _uid_dict_initialize(self):
        """
        Initialize ``self._uid_dict`` with UIDs extracted from the plans in the queue.
        """
        pq = await self._get_plan_queue()
        self._uid_dict_clear()
        # Go over all plans in the queue
        for plan in pq:
            self._uid_dict_add(plan)
        # If plan is currently running
        plan = await self._get_running_plan_info()
        if plan:
            self._uid_dict_add(plan)

    # -------------------------------------------------------------
    #                   Currently Running Plan

    async def _is_plan_running(self):
        """
        See ``self.is_plan_running()`` method.
        """
        return bool(await self._get_running_plan_info())

    async def is_plan_running(self):
        """
        Check if a plan is set as running. True does not indicate that the plan is actually running.

        Returns
        -------
        boolean
            True - a plan is set as running, False otherwise.
        """
        async with self._lock:
            return await self._is_plan_running()

    async def _get_running_plan_info(self):
        """
        See ``self._get_running_plan_info()`` method.
        """
        plan = await self._r_pool.get(self._name_running_plan)
        return json.loads(plan) if plan else {}

    async def get_running_plan_info(self):
        """
        Read info on the currently running plan from Redis.

        Returns
        -------
        dict
            Dictionary representing currently running plan. Empty dictionary if
            no plan is currently running (key value is ``{}`` or the key does not exist).
        """
        async with self._lock:
            return await self._get_running_plan_info()

    async def _set_running_plan_info(self, plan):
        """
        Write info on the currently running plan to Redis

        Parameters
        ----------
        plan: dict
            dictionary that contains plan parameters
        """
        await self._r_pool.set(self._name_running_plan, json.dumps(plan))

    async def _clear_running_plan_info(self):
        """
        Clear info on the currently running plan in Redis.
        """
        await self._set_running_plan_info({})

    # -------------------------------------------------------------
    #                       Plan Queue

    async def _get_plan_queue_size(self):
        """
        See ``self.get_plan_queue_size()`` method.
        """
        return await self._r_pool.llen(self._name_plan_queue)

    async def get_plan_queue_size(self):
        """
        Get the number of plans in the queue.

        Returns
        -------
        int
            The number of plans in the queue.
        """
        async with self._lock:
            return await self._get_plan_queue_size()

    async def _get_plan_queue(self):
        """
        See ``self.get_plan_queue()`` method.
        """
        all_plans_json = await self._r_pool.lrange(self._name_plan_queue, 0, -1)
        return [json.loads(_) for _ in all_plans_json]

    async def get_plan_queue(self):
        """
        Get the list of all plans in the queue. The first element of the list is the first
        plan in the queue.

        Returns
        -------
        list(dict)
            The list of plans in the queue. Each plan is represented as a dictionary.
            Empty list is returned if the queue is empty.
        """
        async with self._lock:
            return await self._get_plan_queue()

    async def _get_plan(self, *, pos=None, uid=None):
        """
        See ``self.get_plan()`` method.
        """
        if (pos is not None) and (uid is not None):
            raise ValueError("Ambiguous parameters: plan position and UID is specified")

        if uid is not None:
            if not self._is_uid_in_dict(uid):
                raise IndexError(f"Plan with UID '{uid}' is not in the queue.")
            running_plan = await self._get_running_plan_info()
            if running_plan and (uid == running_plan["plan_uid"]):
                raise IndexError("The plan with UID '{uid}' is currently running.")
            plan = self._uid_dict_get_plan(uid)

        else:
            pos = pos if pos is not None else "back"

            if pos == "back":
                index = -1
            elif pos == "front":
                index = 0
            elif isinstance(pos, int):
                index = pos
            else:
                raise TypeError(f"Parameter 'pos' has incorrect type: pos={str(pos)} (type={type(pos)})")

            plan_json = await self._r_pool.lindex(self._name_plan_queue, index)
            if plan_json is None:
                raise IndexError(f"Index '{index}' is out of range (parameter pos = '{pos}')")

            plan = json.loads(plan_json) if plan_json else {}

        return plan

    async def get_plan(self, *, pos=None, uid=None):
        """
        Get plan at a given position or with a given UID. If UID is specified, then
        the position is ignored.

        Parameters
        ----------
        pos: int, str or None
            Position of the element ``(0, ..)`` or ``(-1, ..)``, ``front`` or ``back``.

        uid: str or None
            Plan UID of the plan to be retrieved. UID always overrides position.

        Returns
        -------
        dict
            Dictionary of plan parameters.

        Raises
        ------
        TypeError
            Incorrect value of ``pos`` (most likely a string different from ``front`` or ``back``)
        IndexError
            No element with position ``pos`` exists in the queue (index is out of range).
        """
        async with self._lock:
            return await self._get_plan(pos=pos, uid=uid)

    async def _remove_plan(self, plan, single=True):
        """
        Remove exactly a plan from the queue. If ``single=True`` then the exception is
        raised in case of no or multiple matching plans are found in the queue.
        The function is not part of user API and shouldn't be used on exception from
        the other methods of the class.

        Parameters
        ----------
        plan: dict
            Dictionary of plan parameters. Must be identical to the plan that is
            expected to be deleted.
        single: boolean
            True - RuntimeError exception is raised if no or more than one matching
            plan is found, the plans are removed anyway; False - no exception is
            raised.

        Raises
        ------
        RuntimeError
            No or multiple matching plans are removed and ``single=True``.
        """
        n_rem_plans = await self._r_pool.lrem(self._name_plan_queue, 0, json.dumps(plan))
        if (n_rem_plans != 1) and single:
            raise RuntimeError(
                f"The number of removed plans is {n_rem_plans}. One plans is expected to be removed."
            )

    async def _pop_plan_from_queue(self, *, pos=None, uid=None):
        """
        See ``self._pop_plan_from_queue()`` method
        """

        if (pos is not None) and (uid is not None):
            raise ValueError("Ambiguous parameters: plan position and UID is specified")

        pos = pos if pos is not None else "back"

        if uid is not None:
            if not self._is_uid_in_dict(uid):
                raise IndexError(f"Plan with UID '{uid}' is not in the queue.")
            running_plan = await self._get_running_plan_info()
            if running_plan and (uid == running_plan["plan_uid"]):
                raise IndexError("Can not remove a plan which is currently running.")
            plan = self._uid_dict_get_plan(uid)
            await self._remove_plan(plan)
        elif pos == "back":
            plan_json = await self._r_pool.rpop(self._name_plan_queue)
            if plan_json is None:
                raise IndexError("Queue is empty")
            plan = json.loads(plan_json) if plan_json else {}
        elif pos == "front":
            plan_json = await self._r_pool.lpop(self._name_plan_queue)
            if plan_json is None:
                raise IndexError("Queue is empty")
            plan = json.loads(plan_json) if plan_json else {}
        elif isinstance(pos, int):
            plan = await self._get_plan(pos=pos)
            if plan:
                await self._remove_plan(plan)
        else:
            raise ValueError(f"Parameter 'pos' has incorrect value: pos={str(pos)} (type={type(pos)})")

        if plan:
            self._uid_dict_remove(plan["plan_uid"])

        qsize = await self._get_plan_queue_size()

        return plan, qsize

    async def pop_plan_from_queue(self, *, pos=None, uid=None):
        """
        Pop a plan from the queue. Raises ``IndexError`` if plan with index ``pos`` is unavailable
        or if the queue is empty.

        Parameters
        ----------
        pos: int or str or None
            Integer index specified position in the queue. Available string values: "front" or "back".
            The range for the index is ``-qsize..qsize-1``: ``0, -qsize`` - front element of the queue,
            ``-1, qsize-1`` - back element of the queue. If ``pos`` is ``None``, then the plan is popped
            from the back of the queue.

        Returns
        -------
        dict or None
            The last plan in the queue represented as a dictionary.

        Raises
        ------
        ValueError
            Incorrect value of the parameter ``pos`` (typically unrecognized string).
        IndexError
            Position ``pos`` does not exist or the queue is empty.
        """
        async with self._lock:
            return await self._pop_plan_from_queue(pos=pos, uid=uid)

    async def _add_plan_to_queue(self, plan, *, pos=None, before_uid=None, after_uid=None):
        """
        See ``self.add_plan_to_queue()`` method.
        """
        if (pos is not None) and (before_uid is not None or after_uid is not None):
            raise ValueError("Ambiguous parameters: plan position and UID is specified")

        if (before_uid is not None) and (after_uid is not None):
            raise ValueError(
                "Ambiguous parameters: request to insert " "the plan before and after the reference plan"
            )

        pos = pos if pos is not None else "back"

        if "plan_uid" not in plan:
            plan = self.set_new_plan_uuid(plan)
        else:
            self._verify_plan(plan)

        qsize0 = await self._get_plan_queue_size()
        if (before_uid is not None) or (after_uid is not None):
            uid = before_uid if before_uid is not None else after_uid
            before = uid == before_uid

            if not self._is_uid_in_dict(uid):
                raise IndexError(f"Plan with UID '{uid}' is not in the queue.")
            running_plan = await self._get_running_plan_info()
            if running_plan and (uid == running_plan["plan_uid"]):
                if before:
                    raise IndexError("Can not insert a plan in the queue before a currently running plan.")
                else:
                    # Push to the plan front of the queue (after the running plan).
                    qsize = await self._r_pool.lpush(self._name_plan_queue, json.dumps(plan))
            else:
                plan_to_displace = self._uid_dict_get_plan(uid)
                before = uid == before_uid
                qsize = await self._r_pool.linsert(
                    self._name_plan_queue, json.dumps(plan_to_displace), json.dumps(plan), before=before
                )
        elif pos == "back" or (isinstance(pos, int) and pos >= qsize0):
            qsize = await self._r_pool.rpush(self._name_plan_queue, json.dumps(plan))
        elif pos == "front" or (isinstance(pos, int) and (pos == 0 or pos <= -qsize0)):
            qsize = await self._r_pool.lpush(self._name_plan_queue, json.dumps(plan))
        elif isinstance(pos, int):
            # Put the position in the range
            plan_to_displace = await self._get_plan(pos=pos)
            if plan_to_displace:
                qsize = await self._r_pool.linsert(
                    self._name_plan_queue, json.dumps(plan_to_displace), json.dumps(plan), before=True
                )
            else:
                raise RuntimeError(f"Could not find an existing plan at {pos}. Queue size: {qsize0}")
        else:
            raise ValueError(f"Parameter 'pos' has incorrect value: pos='{str(pos)}' (type={type(pos)})")

        self._uid_dict_add(plan)
        return plan, qsize

    async def add_plan_to_queue(self, plan, *, pos=None, before_uid=None, after_uid=None):
        """
        Add the plan to the back of the queue. If position is integer, it is
        clipped to fit within the range of meaningful indices. For the index
        too large or too low, the plan is pushed to the front or the back of the queue.

        Parameters
        ----------
        plan: dict
            Plan represented as a dictionary of parameters

        pos: int, str or None
            Integer that specifies the position index, "front" or "back".
            If ``pos`` is in the range ``1..qsize-1`` the plan is inserted
            to the specified position and plans at positions ``pos..qsize-1``
            are shifted by one position to the right. If ``-qsize<pos<0`` the
            plan is inserted at the positon counting from the back of the queue
            (-1 - the last element of the queue). If ``pos>=qsize``,
            the plan is added to the back of the queue. If ``pos==0`` or
            ``pos<=-qsize``, the plan is pushed to the front of the queue.

        before_uid: str or None
            If UID is specified, then the plan is inserted before the plan with UID.
            ``before_uid`` has precedence over ``after_uid``.

        after_uid: str or None
            If UID is specified, then the plan is inserted before the plan with UID.

        Returns
        -------
        dict, int
            The dictionary that contains a plan that was added and the new size of the queue.

        Raises
        ------
        ValueError
            Incorrect value of the parameter ``pos`` (typically unrecognized string).
        TypeError
            Incorrect type of ``plan`` (should be dict)
        """
        async with self._lock:
            return await self._add_plan_to_queue(plan, pos=pos, before_uid=before_uid, after_uid=after_uid)

    async def _move_plan(self, *, pos=None, uid=None, pos_dest=None, before_uid=None, after_uid=None):
        """
        See ``self.move_plan()`` method.
        """
        if (pos is None) and (uid is None):
            raise ValueError("Source position or UID is not specified.")
        if (pos_dest is None) and (before_uid is None) and (after_uid is None):
            raise ValueError("Destination position or UID is not specified.")

        if (pos is not None) and (uid is not None):
            raise ValueError("Ambiguous parameters: Both position and uid is specified for the source plan.")
        if (pos_dest is not None) and (before_uid is not None or after_uid is not None):
            raise ValueError("Ambiguous parameters: Both position and uid is specified for the destination plan.")
        if (before_uid is not None) and (after_uid is not None):
            raise ValueError("Ambiguous parameters: source should be moved 'before' and 'after' the destination.")

        queue_size = await self._get_plan_queue_size()

        # Find the source plan
        src_txt = ""
        src_by_index = False  # Indicates that the source is addressed by index
        try:
            if uid is not None:
                src_txt = f"UID '{uid}'"
                plan_source = await self._get_plan(uid=uid)
            else:
                src_txt = f"position {pos}"
                src_by_index = True
                plan_source = await self._get_plan(pos=pos)
        except Exception as ex:
            raise IndexError(f"Source plan ({src_txt}) was not found: {str(ex)}.")

        uid_source = plan_source["plan_uid"]

        # Find the destination plan
        dest_txt, before = "", True
        try:
            if (before_uid is not None) or (after_uid is not None):
                uid_dest = before_uid if before_uid else after_uid
                before = uid_dest == before_uid
                dest_txt = f"UID '{uid_dest}'"
                plan_dest = await self._get_plan(uid=uid_dest)
            else:
                dest_txt = f"position {pos_dest}"
                plan_dest = await self._get_plan(pos=pos_dest)

                # Find the index of the source in the most efficient way
                src_index = pos if src_by_index else (await self._get_index_by_uid(uid=uid))
                if src_index == "front":
                    src_index = 0
                elif src_index == "back":
                    src_index = queue_size - 1

                # Determine if the item must be inserted before or after the destination
                if pos_dest == "front":
                    before = True
                elif pos_dest == "back":
                    # This is one case when we need to insert the plan after the 'destination' plan.
                    before = False
                else:
                    before = src_index > pos_dest

        except Exception as ex:
            raise IndexError(f"Destination plan ({dest_txt}) was not found: {str(ex)}.")

        # Copy destination UID from the plan (we need it for the case of if addressing is positional
        #   so we convert it to UID, but we can do it for the case of UID addressing as well)
        #   In case of positional addressing 'before' is True, so the source is going to be
        #   inserted in place of destination.
        uid_dest = plan_dest["plan_uid"]

        # If source and destination point to the same plan, then do nothing,
        #   but consider it a valid operation.
        if uid_source != uid_dest:
            plan, _ = await self._pop_plan_from_queue(uid=uid_source)
            kw = {"before_uid": uid_dest} if before else {"after_uid": uid_dest}
            kw.update({"plan": plan})
            plan, qsize = await self._add_plan_to_queue(**kw)
        else:
            plan = plan_dest
            qsize = await self._get_plan_queue_size()
        return plan, qsize

    async def move_plan(self, *, pos=None, uid=None, pos_dest=None, before_uid=None, after_uid=None):
        """
        Move existing plan within the queue.

        Parameters
        ----------
        pos: str or int
            Position of the source plan: positive or negative integer that specifieds the index
            of the plan in the queue or a string from the set {"back", "front"}.
        uid: str
            UID of the source plan. UID overrides the position
        pos_dext: str or int
            Index of the new position of the plan in the queue: positive or negative integer that
            specifieds the index of the plan in the queue or a string from the set {"back", "front"}.
        before_uid: str
            Insert the plan before the plan with the given UID.
        after_uid: str
            Insert the plan after the plan with the given UID.

        Returns
        -------
        dict, int
            The dictionary that contains a plan that was moved and the size of the queue.

        Raises
        ------
        ValueError
            Error in specification of source or destination.
        """
        async with self._lock:
            return await self._move_plan(
                pos=pos, uid=uid, pos_dest=pos_dest, before_uid=before_uid, after_uid=after_uid
            )

    async def _clear_plan_queue(self):
        """
        See ``self.clear_plan_queue()`` method.
        """
        while await self._get_plan_queue_size():
            await self._pop_plan_from_queue()

    async def clear_plan_queue(self):
        """
        Remove all entries from the plan queue. Does not touch the running plan.
        The plan may be pushed back into the queue if it is stopped.
        """
        async with self._lock:
            await self._clear_plan_queue()

    # -----------------------------------------------------------------------
    #                          Plan History

    async def _add_plan_to_history(self, plan):
        """
        Add the plan to history.

        Parameters
        ----------
        plan: dict
            Plan represented as a dictionary of parameters. No verifications are performed
            on the plan. The function is not intended to be used outside of this class.

        Returns
        -------
        int
            The new size of the history.
        """
        history_size = await self._r_pool.rpush(self._name_plan_history, json.dumps(plan))
        return history_size

    async def _get_plan_history_size(self):
        """
        See ``self.get_plan_history_size()`` method.
        """
        return await self._r_pool.llen(self._name_plan_history)

    async def get_plan_history_size(self):
        """
        Get the number of items in the plan history.

        Returns
        -------
        int
            The number of plans in the history.
        """
        async with self._lock:
            return await self._get_plan_history_size()

    async def _get_plan_history(self):
        """
        See ``self.get_plan_history()`` method.
        """
        all_plans_json = await self._r_pool.lrange(self._name_plan_history, 0, -1)
        return [json.loads(_) for _ in all_plans_json]

    async def get_plan_history(self):
        """
        Get the list of all plans in the history. The first element of the list is
        the oldest history entry.

        Returns
        -------
        list(dict)
            The list of plans in the queue. Each plan is represented as a dictionary.
            Empty list is returned if the queue is empty.
        """
        async with self._lock:
            return await self._get_plan_history()

    async def _clear_plan_history(self):
        """
        See ``self.clear_plan_history()`` method.
        """
        while await self._get_plan_history_size():
            await self._r_pool.rpop(self._name_plan_history)

    async def clear_plan_history(self):
        """
        Remove all entries from the plan queue. Does not touch the running plan.
        The plan may be pushed back into the queue if it is stopped.
        """
        async with self._lock:
            await self._clear_plan_history()

    # ----------------------------------------------------------------------
    #          Standard plan operations during queue execution

    async def _set_next_plan_as_running(self):
        """
        See ``self.set_next_plan_as_running()`` method.
        """
        # UID remains in the `self._uid_dict` after this operation.
        if not await self._is_plan_running():
            plan_json = await self._r_pool.lpop(self._name_plan_queue)
            if plan_json:
                plan = json.loads(plan_json)
                await self._set_running_plan_info(plan)
            else:
                plan = {}
        else:
            plan = {}
        return plan

    async def set_next_plan_as_running(self):
        """
        Sets the next plan from the queue as a running plan. The plan is removed
        from the queue. UID remains in ``self._uid_dict``, i.e. plan with the same UID
        may not be added to the queue while it is being executed.

        Returns
        -------
        dict
            The plan that was set as currently running. If another plan is currently
            running or the queue is empty, then ``{}`` is returned.
        """
        async with self._lock:
            return await self._set_next_plan_as_running()

    async def _set_processed_plan_as_completed(self, exit_status):
        """
        See ``self.set_processed_plan_as_completed`` method.
        """
        # Note: UID remains in the `self._uid_dict` after this operation
        if await self._is_plan_running():
            plan = await self._get_running_plan_info()
            plan["exit_status"] = exit_status
            await self._clear_running_plan_info()
            self._uid_dict_remove(plan["plan_uid"])
            await self._add_plan_to_history(plan)
        else:
            plan = {}
        return plan

    async def set_processed_plan_as_completed(self, exit_status):
        """
        Moves currently executed plan to history and sets ``exit_status`` key.
        UID is removed from ``self._uid_dict``, so a copy of the plan with
        the same UID may be added to the queue.

        Parameters
        ----------
        exit_status: str
            Completion status of the plan.

        Returns
        -------
        dict
            The plan added to the history including ``exit_status``. If another no plan is currently
            running, then ``{}`` is returned.
        """
        async with self._lock:
            return await self._set_processed_plan_as_completed(exit_status=exit_status)

    async def _set_processed_plan_as_stopped(self, exit_status):
        """
        See ``self.set_prcessed_plan_as_stopped()`` method.
        """
        # Note: UID is removed from `self._uid_dict`.
        if await self._is_plan_running():
            plan = await self._get_running_plan_info()
            plan["exit_status"] = exit_status
            await self._clear_running_plan_info()
            await self._r_pool.lpush(self._name_plan_queue, json.dumps(plan))
            self._uid_dict_update(plan)
            await self._add_plan_to_history(plan)
        else:
            plan = {}
        return plan

    async def set_processed_plan_as_stopped(self, exit_status):
        """
        Pushes currently executed plan to the beginning of the queue and adds
        it to history with additional sets ``exit_status`` key.
        UID is remains in ``self._uid_dict``.

        Parameters
        ----------
        exit_status: str
            Completion status of the plan.

        Returns
        -------
        dict
            The plan added to the history including ``exit_status``. If another no plan is currently
            running, then ``{}`` is returned.
        """
        async with self._lock:
            return await self._set_processed_plan_as_stopped(exit_status=exit_status)
